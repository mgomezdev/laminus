"""Slicing: the OrcaSlicer subprocess task behind a job, the slice/prepared and thumbnail routes, artifact download.

Moved out of app/main.py. Everything that lives there (state such as `jobs` and `catalog`, config, shared helpers, and
the other route modules' functions, which main re-exports) is read as `main.<name>` at call time, so a test that patches
`app.main.<name>` still takes effect here. Routes register straight on `main.app`. See app/routes/README.md.
"""
import os
import shutil
import asyncio
import signal
import uuid
import glob
import re
import time
import json
import zipfile
from typing import Optional
from fastapi import UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, Response
from app.project_config_builder import build_project_settings, embed_project_settings
from app.plate_extract import (
    extract_plate as _extract_plate, retarget_plate as _retarget_plate,
    PlateExtractError as _PlateExtractError, PlateFitError as _PlateFitError,
)
from app.stl_to_3mf import stl_to_3mf as _stl_to_3mf, strip_application_version as _strip_app_version

import app.main as main


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill the whole process group spawned for *process*, not just the direct child.

    xvfb-run (from the `xvfb` apt package, see Dockerfile) is a /bin/sh wrapper -
    SIGKILL on it alone cannot be trapped, so Xvfb and the orcaslicer child are
    reparented to PID 1 and keep running. Requires the process to have been
    started with start_new_session=True.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError, OSError, TypeError):
        process.kill()
    await process.wait()


async def _stream_subprocess_output(process: asyncio.subprocess.Process, job_logger: main.JobLogger):
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        job_logger.log(line.decode("utf-8", errors="replace"))


async def _strip_model_settings(src: str, dst: str) -> None:
    """Write dst as a copy of src with Metadata/model_settings.config removed."""
    import zipfile as _zf
    def _do_strip():
        with _zf.ZipFile(src, "r") as s:
            with _zf.ZipFile(dst, "w", compression=_zf.ZIP_DEFLATED) as d:
                for item in s.infolist():
                    if item.filename != "Metadata/model_settings.config":
                        d.writestr(item, s.read(item.filename))
    await asyncio.to_thread(_do_strip)


def _sliced_plate_gcode_error(threemf: str, plate_id: int) -> Optional[str]:
    """None if the exported 3MF holds G-code for the requested plate (any plate when plate_id==0)."""
    try:
        with zipfile.ZipFile(threemf) as z:
            names = z.namelist()
    except Exception as exc:
        return f"exported 3MF unreadable: {exc}"
    want = f"Metadata/plate_{plate_id}.gcode"
    if plate_id > 0 and want in names:
        return None
    if plate_id == 0 and any(re.fullmatch(r"Metadata/plate_\d+\.gcode", n) for n in names):
        return None
    which = f"plate {plate_id}" if plate_id > 0 else "any plate"
    return (f"OrcaSlicer reported success but produced no G-code for {which} "
            f"(plate missing or empty in the project).")


async def run_orcaslicer_task(
    job_id: str,
    input_file_path: str,
    output_dir: str,
    plate_id: int = 1,
    export_3mf: Optional[str] = None,
    geometry_only_retry: bool = False,
    target_settings: Optional[dict] = None,
):
    """*target_settings*: the resolved machine/process/filament project config, embedded only
    after the plate is extracted from the source project (BIZ-355). None (``/slice/prepared``)
    slices the input's own embedded settings."""
    job = main.jobs.get(job_id)
    if job is None:
        return
    job_logger = job["logger"]
    job["status"] = "slicing"
    main._save_jobs()
    main._history_upsert(job_id, status="slicing", updated_at=time.time())
    job_logger.log(f"Starting slice: {os.path.basename(input_file_path)}")

    def _fail(error: str, sentinel: str) -> None:
        job["status"] = "failed"
        job["error"] = error
        main._save_jobs()
        main._history_upsert(job_id, status="failed", error=error, updated_at=time.time())
        job_logger.log(f"ERROR: {error}")
        job_logger.log(f"__FAILED__: {sentinel}")

    # Orca slices plate 1 of the single-plate project built below; `arrange` is decided
    # once extraction tells us whether the input carried a plate layout.
    slice_plate = plate_id
    arrange = "1"

    async def _attempt(slice_input: str, label: str) -> bool:
        cmd = [
            "xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
            "orcaslicer", "--slice", str(slice_plate),
            "--outputdir", output_dir, "--arrange", arrange,
        ]
        if export_3mf:
            cmd.extend(["--export-3mf", export_3mf])
        cmd.append(slice_input)
        job_logger.log(f"{label}: {' '.join(cmd)}")
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(main._stream_subprocess_output(process, job_logger), timeout=main.SLICE_TIMEOUT)
            except asyncio.TimeoutError:
                await main._kill_process_group(process)
                job_logger.log(f"ERROR: Timed out after {main.SLICE_TIMEOUT}s")
                return False
            else:
                await process.wait()
                return process.returncode == 0
        except Exception as exc:
            job_logger.log(f"SYSTEM ERROR: {exc}")
            main.logger.exception("Subprocess error in job %s", job_id)
            return False

    # OrcaSlicer 2.3.2 on Linux segfaults when the 3MF's Application metadata
    # contains a version string (e.g. "BambuStudio-2.3.2"). Strip it first.
    if input_file_path.lower().endswith(".3mf"):
        fixed_path = input_file_path[:-4] + "_fixed.3mf"
        try:
            for note in await asyncio.to_thread(_strip_app_version, input_file_path, fixed_path):
                job_logger.log(f"Rewrote project setting {note}")
            input_file_path = fixed_path
        except Exception as exc:
            job_logger.log(f"WARNING: could not strip Application version: {exc}")

    # BIZ-349: never hand Orca a multi-plate project. `--arrange 1` re-packs objects across
    # plates (plates collapse, parts migrate off the requested plate and silently vanish
    # from the job) and `--arrange 0` checks plate bounds against a stride that does not
    # always match the project's. Pull the requested plate - plate 1 included - into its own
    # single-plate project at plate-1 coordinates and slice that with `--arrange 0`.
    #
    # BIZ-355: with target settings, the plate is located with the SOURCE project's bed (the
    # grid it was laid out on), then moved from the source bed centre to the target's,
    # checked against the target bed, and only then given the target settings.
    if input_file_path.lower().endswith(".3mf") and plate_id >= 1:
        single_path = input_file_path[:-4] + f"_plate{plate_id}.3mf"
        extracted = single_path if target_settings is None else single_path[:-4] + "_src.3mf"
        placed = None
        try:
            info = await asyncio.to_thread(
                _extract_plate, input_file_path, plate_id, extracted, target_settings)
            if target_settings is not None and info["has_plate_data"]:
                placed = await asyncio.to_thread(
                    _retarget_plate, extracted, single_path, plate_id, info["source_bed"],
                    target_settings)
            elif target_settings is not None:
                await asyncio.to_thread(embed_project_settings, extracted, target_settings, single_path)
        except _PlateFitError as exc:
            main.logger.warning("Job %s: %s", job_id, exc)
            _fail(str(exc), "Plate does not fit the target bed")
            return
        except _PlateExtractError as exc:
            main.logger.warning("Job %s: plate extraction rejected plate %d: %s", job_id, plate_id, exc)
            _fail(f"Cannot slice plate {plate_id}: {exc}", "Plate extraction failed")
            return
        except Exception as exc:
            main.logger.exception("Job %s: plate extraction crashed", job_id)
            _fail(f"Could not extract plate {plate_id}: {exc}", "Plate extraction failed")
            return
        main.logger.info("Job %s plate extraction: %s", job_id, json.dumps(info))
        if info["has_plate_data"]:
            job_logger.log(
                f"Plate extraction: plate {plate_id} of {info['source_plates']} -> single-plate project; "
                f"{info['kept_items']} build item(s) kept, {info['dropped_items']} dropped, "
                f"shift {info['origin_shift']} mm (bed {info['bed_mm']}), objects {info['objects']}")
            cg = info.get("custom_gcode")
            if cg:
                job_logger.log(f"Plate extraction: custom layer G-code (pauses/colour changes): "
                               f"{cg['kept']} kept for plate {plate_id}, other plates' blocks "
                               f"dropped {cg['dropped_plates']}")
            if info["missing_instances"]:
                job_logger.log(f"WARNING: {info['missing_instances']} instance(s) listed on plate "
                               f"{plate_id} have no build item and were skipped")
            for p in info["positions"]:
                job_logger.log(f"  object {p['object_id']}#{p['instance']} at x={p['x']} y={p['y']}")
            if placed:
                main.logger.info("Job %s plate placement: %s", job_id, json.dumps(placed))
                job_logger.log(
                    f"Plate placement: source bed {info['source_bed']} -> target bed "
                    f"{placed['target_bed_mm']} mm; footprint {placed['footprint_mm']} mm; "
                    f"{placed['placement']} shift {placed['shift']} mm")
            arrange = "0"
        else:
            # No plate layout (bare STL-derived 3MF): nothing to collapse, but parts still
            # need placing, so let Orca arrange them.
            job_logger.log("Plate extraction: input declares no plates; slicing as-is with --arrange 1")
        input_file_path = single_path
        slice_plate = 1

    success = await _attempt(input_file_path, "Attempt 1")

    used_geometry_only = False
    if not success and geometry_only_retry:
        job_logger.log(
            "WARNING: Attempt 1 failed - geometry_only_retry=true, retrying with model_settings.config "
            "REMOVED. Per-object/per-part overrides, modifiers, layer ranges, extruder assignment and "
            "plate settings will NOT be applied to this slice.")
        main.logger.warning("Job %s: attempt 1 failed, geometry-only retry requested", job_id)
        base, ext = os.path.splitext(input_file_path)
        geo_path = base + "_geo" + ext
        try:
            await main._strip_model_settings(input_file_path, geo_path)
            success = await _attempt(geo_path, "Attempt 2 (geometry-only)")
            used_geometry_only = success
        except Exception as exc:
            job_logger.log(f"ERROR stripping model_settings: {exc}")
    elif not success:
        job_logger.log("Attempt 1 failed (geometry_only_retry not requested; no fallback attempted)")

    if used_geometry_only:
        job_logger.log("WARNING: output was produced by the geometry-only fallback; per-object "
                       "settings from the project were discarded")
        main.logger.warning("Job %s: completed via geometry-only fallback (per-object settings discarded)",
                       job_id)

    if success:
        if export_3mf:
            target = os.path.join(output_dir, export_3mf)
            found = target if os.path.exists(target) else None
            empty = main._sliced_plate_gcode_error(target, slice_plate) if found else None
            if empty:
                main.logger.warning("Job %s: %s (requested plate %d, sliced plate %d)",
                               job_id, empty, plate_id, slice_plate)
                _fail(empty, "No G-code produced")
                return
        else:
            gcodes = sorted(glob.glob(os.path.join(output_dir, "*.gcode")))
            found = gcodes[0] if gcodes else None

        if found:
            job["status"] = "completed"
            job["sliced_file"] = found
            main._save_jobs()
            main._history_upsert(job_id, status="completed", sliced_file=os.path.basename(found),
                             error=None, updated_at=time.time())
            job_logger.log(f"Output: {os.path.basename(found)}")
            job_logger.log("__COMPLETED__")
        else:
            job["status"] = "failed"
            job["error"] = "OrcaSlicer succeeded but no output file found."
            main._save_jobs()
            main._history_upsert(job_id, status="failed", error=job["error"], updated_at=time.time())
            job_logger.log("ERROR: No output file found.")
            job_logger.log("__FAILED__: Missing output file")
    else:
        job["status"] = "failed"
        job["error"] = "OrcaSlicer slice process failed. See logs."
        main._save_jobs()
        main._history_upsert(job_id, status="failed", error=job["error"], updated_at=time.time())
        job_logger.log("__FAILED__: OrcaSlicer returned non-zero")


async def start_slice(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    machine_uuid: Optional[str] = Form(None),
    manufacturer: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    nozzle: Optional[str] = Form(None),
    process_uuid: str = Form(...),
    filament_uuids: str = Form(..., description='JSON array, e.g. ["uuid1"]'),
    plate: int = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: Optional[str] = Form(None, description=main.GEOMETRY_ONLY_RETRY_DESC),
    extra_config: Optional[str] = Form(None, description='JSON object merged into project settings after resolution'),
):
    geometry_only_retry = main._form_default_false(geometry_only_retry)
    if main.catalog is None:
        raise HTTPException(status_code=503, detail="Profile catalog not yet ready.")

    # Count "pending" too: a job stays "pending" until its background task (which
    # Starlette only runs after the response is sent) flips it to "slicing", so a
    # burst of concurrent requests would otherwise all see active_count == 0.
    active_count = sum(1 for j in main.jobs.values() if j["status"] in ("pending", "slicing"))
    if active_count >= main.MAX_CONCURRENT_JOBS:
        raise HTTPException(status_code=503, detail=f"Too many active jobs ({active_count}/{main.MAX_CONCURRENT_JOBS}). Retry later.")

    try:
        fil_uuid_list: list[str] = json.loads(filament_uuids)
        if not isinstance(fil_uuid_list, list) or not fil_uuid_list:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="filament_uuids must be a non-empty JSON array.")

    if plate < 1:
        raise HTTPException(status_code=422, detail="plate must be >= 1.")

    if export_3mf:
        try:
            export_3mf = main._safe_filename(export_3mf)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if not export_3mf.lower().endswith(".3mf"):
            raise HTTPException(status_code=422, detail="export_3mf must end with .3mf")

    # Machine lookup: prefer stable UUID, fall back to (manufacturer, model, nozzle) tuple.
    if machine_uuid:
        machine_entry = main.catalog.get_by_uuid(machine_uuid)
        if machine_entry is None or machine_entry.get("type") != "machine":
            raise HTTPException(status_code=422, detail=f"Machine UUID '{machine_uuid}' not found in catalog.")
    elif all(x is not None for x in (manufacturer, model, nozzle)):
        machine_entry = main.catalog.get_machine(manufacturer, model, nozzle)
        if machine_entry is None:
            raise HTTPException(
                status_code=422,
                detail=f"No machine profile found for manufacturer='{manufacturer}' model='{model}' nozzle='{nozzle}'.",
            )
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide machine_uuid OR all three of manufacturer, model, and nozzle.",
        )

    process_entry = main.catalog.get_by_uuid(process_uuid)
    if process_entry is None or process_entry.get("type") != "process":
        raise HTTPException(status_code=422, detail=f"Process UUID '{process_uuid}' not found.")

    filament_entries = []
    for fuid in fil_uuid_list:
        fe = main.catalog.get_by_uuid(fuid)
        if fe is None or fe.get("type") != "filament":
            raise HTTPException(status_code=422, detail=f"Filament UUID '{fuid}' not found.")
        filament_entries.append(fe)

    machine_name = machine_entry["name"]
    compat = process_entry.get("compatible_printers", [])
    if compat and machine_name not in compat:
        raise HTTPException(
            status_code=422,
            detail=f"Process '{process_entry['name']}' is not compatible with '{machine_name}'.",
        )

    try:
        safe_name = main._safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(main.JOBS_DIR, job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    raw_path = os.path.join(input_dir, safe_name)
    with open(raw_path, "wb") as buf:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buf)

    if safe_name.lower().endswith(".stl"):
        base_3mf = os.path.join(input_dir, os.path.splitext(safe_name)[0] + ".3mf")
        try:
            await asyncio.to_thread(_stl_to_3mf, raw_path, base_3mf)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not parse uploaded STL: {e}")
    else:
        base_3mf = raw_path

    machine_resolved = main.catalog.resolved(machine_entry["uuid"])
    process_resolved = main.catalog.resolved(process_entry["uuid"])
    filament_resolved_list = [main.catalog.resolved(fe["uuid"]) for fe in filament_entries]

    project_cfg = await asyncio.to_thread(
        build_project_settings, machine_resolved, process_resolved, filament_resolved_list
    )
    if extra_config:
        try:
            overrides = json.loads(extra_config)
            if not isinstance(overrides, dict):
                raise ValueError
            project_cfg.update(overrides)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="extra_config must be a JSON object.")
    # STL: no plate layout to preserve, embed now. 3MF: the slice task extracts the plate with
    # the upload's own bed before embedding project_cfg (BIZ-355), so only validate it here.
    target_settings = None
    if base_3mf == raw_path and raw_path.lower().endswith(".3mf"):
        prepared_3mf, target_settings = raw_path, project_cfg
        try:
            def _check_zip(path: str) -> None:
                with zipfile.ZipFile(path) as zf:
                    bad = zf.testzip()
                if bad:
                    raise ValueError(f"corrupt member {bad}")
            await asyncio.to_thread(_check_zip, raw_path)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Uploaded file is not a valid 3MF archive: {e}")
    else:
        prepared_3mf = os.path.join(input_dir, "prepared.3mf")
        try:
            await asyncio.to_thread(embed_project_settings, base_3mf, project_cfg, prepared_3mf)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Uploaded file is not a valid 3MF archive: {e}")

    job_logger = main.JobLogger(job_id)
    _wall_now = time.time()
    output_format = "gcode_3mf" if export_3mf else "gcode"
    main.jobs[job_id] = {
        "id": job_id, "status": "pending",
        "input_file": prepared_3mf, "output_dir": output_dir,
        "sliced_file": None, "output_format": output_format,
        "error": None, "logger": job_logger, "created_at": time.monotonic(),
        "_wall_created_at": _wall_now,
    }
    main._save_jobs()
    main._history_upsert(job_id, filename=safe_name, status="pending", output_format=output_format,
                     sliced_file=None, error=None, evicted=False,
                     created_at=_wall_now, updated_at=_wall_now, source="slice/start")
    background_tasks.add_task(
        main.run_orcaslicer_task,
        job_id=job_id, input_file_path=prepared_3mf, output_dir=output_dir,
        plate_id=plate, export_3mf=export_3mf, geometry_only_retry=geometry_only_retry,
        target_settings=target_settings,
    )
    return {"job_id": job_id, "status": "pending", "message": "Slicing job started."}


@main.app.post(
    "/api/v1/slice/prepared",
    tags=["slice"],
    summary="Slice a pre-configured 3MF (no profile resolution)",
    description=(
        "Accepts a `.3mf` that already contains `Metadata/project_settings.config` "
        "(e.g., exported from OrcaSlicer desktop or produced by `POST /api/v1/jobs` "
        "with `export_3mf` set). No catalog lookup or profile merging is performed — "
        "OrcaSlicer uses the embedded settings verbatim.\n\n"
        "Use this endpoint when you already have a fully-configured 3MF and want to avoid "
        "the overhead of profile resolution. Typical callers: Themis queue engine dispatching "
        "pre-packed project 3MFs, or debugging with a known-good 3MF from the desktop app.\n\n"
        "**Plate selection:** the requested plate is extracted into its own single-plate "
        "project (moved to plate-1 coordinates, with its own custom layer G-code) and sliced "
        "with `--arrange 0`, so parts are never re-packed across plates. A missing or empty "
        "plate fails the job.\n\n"
        "**Geometry-only retry** is opt-in (`geometry_only_retry=true`, default `false`), as in "
        "`POST /api/v1/jobs`: if the first slice fails, retry once with "
        "`Metadata/model_settings.config` removed, which discards per-object settings. The job "
        "log flags it whenever it is used.\n\n"
        "**`export_3mf`:** when set, OrcaSlicer embeds the sliced GCode into a `.3mf` and "
        "the download endpoint returns that file instead of a standalone `.gcode`.\n\n"
        "The response and job lifecycle are identical to `POST /api/v1/jobs`."
    ),
    responses={
        400: {"description": "Bad filename (null bytes, semicolons, or missing Content-Disposition filename)"},
        422: {"description": "File is not a .3mf, or plate < 1"},
        503: {
            "description": (
                "Too many concurrent slice jobs — retry when a slot opens "
                "(check active_jobs in GET /api/v1/health)"
            ),
        },
    },
    operation_id="v1_slice_prepared")
async def slice_prepared(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    plate: int = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: Optional[str] = Form(None, description=main.GEOMETRY_ONLY_RETRY_DESC),
):
    geometry_only_retry = main._form_default_false(geometry_only_retry)
    if plate < 1:
        raise HTTPException(status_code=422, detail="plate must be >= 1.")

    if export_3mf:
        try:
            export_3mf = main._safe_filename(export_3mf)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if not export_3mf.lower().endswith(".3mf"):
            raise HTTPException(status_code=422, detail="export_3mf must end with .3mf")

    # Count "pending" too: a job stays "pending" until its background task (which
    # Starlette only runs after the response is sent) flips it to "slicing", so a
    # burst of concurrent requests would otherwise all see active_count == 0.
    active_count = sum(1 for j in main.jobs.values() if j["status"] in ("pending", "slicing"))
    if active_count >= main.MAX_CONCURRENT_JOBS:
        raise HTTPException(status_code=503, detail=f"Too many active jobs ({active_count}/{main.MAX_CONCURRENT_JOBS}). Retry later.")

    try:
        safe_name = main._safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=422, detail="Only .3mf files accepted here.")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(main.JOBS_DIR, job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    input_path = os.path.join(input_dir, safe_name)
    with open(input_path, "wb") as buf:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buf)

    job_logger = main.JobLogger(job_id)
    _wall_now = time.time()
    output_format = "gcode_3mf" if export_3mf else "gcode"
    main.jobs[job_id] = {
        "id": job_id, "status": "pending",
        "input_file": input_path, "output_dir": output_dir,
        "sliced_file": None, "output_format": output_format,
        "error": None, "logger": job_logger, "created_at": time.monotonic(),
        "_wall_created_at": _wall_now,
    }
    main._save_jobs()
    main._history_upsert(job_id, filename=safe_name, status="pending", output_format=output_format,
                     sliced_file=None, error=None, evicted=False,
                     created_at=_wall_now, updated_at=_wall_now, source="slice/prepared")
    background_tasks.add_task(
        main.run_orcaslicer_task,
        job_id=job_id, input_file_path=input_path, output_dir=output_dir,
        plate_id=plate, export_3mf=export_3mf, geometry_only_retry=geometry_only_retry,
    )
    return {"job_id": job_id, "status": "pending", "message": "Slice job started."}


@main.app.post(
    "/api/v1/slice/thumbnail",
    tags=["slice"],
    summary="Render a plate thumbnail PNG (synchronous)",
    description=(
        "Runs OrcaSlicer with `--arrange 0 --export-3mf` to render a plate thumbnail PNG "
        "without disturbing the model's geometry or generating GCode. Returns the PNG bytes "
        "directly with `Content-Type: image/png`.\n\n"
        "**Blocking:** waits up to 120 seconds for OrcaSlicer to finish. No job tracking — "
        "this is fire-and-forget from the caller's perspective.\n\n"
        "The endpoint accepts only `.3mf` files. The 3MF must embed a valid plate (the "
        "requested `plate` number must exist in the model). OrcaSlicer writes the PNG to "
        "`Metadata/plate_{N}.png` inside the output 3MF; the endpoint extracts and returns "
        "that PNG, cleaning up all temporary files afterwards.\n\n"
        "All error conditions (OrcaSlicer failure, timeout, missing PNG output) return "
        "**422** with a JSON detail object `{\"error\": \"...\"}` describing the cause."
    ),
    responses={
        422: {
            "description": (
                "OrcaSlicer returned non-zero, no PNG found in output, "
                "plate < 1, bad filename, or non-.3mf file uploaded. "
                "Also returned on timeout (the OrcaSlicer process is killed)."
            ),
        },
    },
    operation_id="v1_slice_thumbnail")
async def slice_thumbnail(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    plate: int = Form(...),
):
    if plate < 1:
        raise HTTPException(status_code=422, detail={"error": "plate must be >= 1."})
    try:
        safe_name = main._safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})
    if not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=422, detail={"error": "Only .3mf files accepted."})

    job_id = str(uuid.uuid4())
    with main._track_inflight_arrange(job_id):
        job_dir = os.path.join(main.ARRANGE_DIR, f"thumb_{job_id}")
        input_dir = os.path.join(job_dir, "input")
        output_dir = os.path.join(job_dir, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        in_file = os.path.join(input_dir, safe_name)
        out_3mf = os.path.join(output_dir, "thumb.3mf")

        with open(in_file, "wb") as buf:
            await asyncio.to_thread(shutil.copyfileobj, file.file, buf)

        cmd = [
            "xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
            "orcaslicer", "--slice", str(plate), "--arrange", "0",
            "--export-3mf", out_3mf, in_file,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=main.THUMBNAIL_TIMEOUT)
            except asyncio.TimeoutError:
                await main._kill_process_group(process)
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=422, detail={"error": f"Timed out after {main.THUMBNAIL_TIMEOUT}s"})

            if process.returncode != 0 or not os.path.exists(out_3mf):
                log_text = stdout.decode("utf-8", errors="replace") if stdout else ""
                main.logger.error("Thumbnail failed. Exit %s. Log: %s", process.returncode, log_text)
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=422, detail={"error": f"OrcaSlicer exited {process.returncode}"})

            def _extract_png(archive: str, plate_idx: int) -> Optional[bytes]:
                import zipfile as _zf
                with _zf.ZipFile(archive, "r") as zf:
                    names = zf.namelist()
                    for candidate in (f"Metadata/plate_{plate_idx}.png", "Metadata/plate_1.png"):
                        if candidate in names:
                            return zf.read(candidate)
                return None

            png_bytes = await asyncio.to_thread(_extract_png, out_3mf, plate)
            background_tasks.add_task(main.cleanup_directory, job_dir)

            if png_bytes is None:
                raise HTTPException(status_code=422, detail={"error": "No plate PNG found in OrcaSlicer output"})

            return Response(content=png_bytes, media_type="image/png")

        except HTTPException:
            raise
        except Exception as exc:
            background_tasks.add_task(main.cleanup_directory, job_dir)
            main.logger.exception("Thumbnail error")
            raise HTTPException(status_code=422, detail={"error": str(exc)})


async def download_sliced_file(job_id: str, background_tasks: BackgroundTasks):
    if job_id not in main.jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    job = main.jobs[job_id]
    if job.get("_stub"):
        # Restored from disk after a restart: the sliced_file (if any) is gone along
        # with the rest of the job's working directory. Report it as gone, not as
        # "not complete" (which is misleading for a job whose status is "completed").
        raise HTTPException(status_code=404, detail="Job data was lost when Laminus restarted.")
    if job["status"] != "completed" or not job["sliced_file"]:
        raise HTTPException(status_code=400, detail=f"Slicing job is not complete. Current status: {job['status']}")
    file_path = job["sliced_file"]
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Sliced file was not found on disk.")
    filename = os.path.basename(file_path)
    # Fix R6: evict job from dict and clean up disk after download
    background_tasks.add_task(main._evict_job, job_id)
    return FileResponse(path=file_path, filename=filename, media_type="application/octet-stream")


def _evict_job(job_id: str):
    """Remove a job from the in-memory dict and clean its working directory."""
    j = main.jobs.pop(job_id, None)
    if j:
        main.cleanup_directory(os.path.join(main.JOBS_DIR, job_id))
        main._history_upsert(job_id, evicted=True, updated_at=time.time())
