"""3MF project routes: arrange, inspect, previews, subset, merge, measure, and the Orca arrange helpers they share.

Moved out of app/main.py. Everything that lives there (state such as `jobs` and `catalog`, config, shared helpers, and
the other route modules' functions, which main re-exports) is read as `main.<name>` at call time, so a test that patches
`app.main.<name>` still takes effect here. Routes register straight on `main.app`. See app/routes/README.md.
"""
import os
import shutil
import asyncio
import uuid
import json
from typing import List, Optional
from fastapi import UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from app import preview_3mf as _preview
from app import subset_3mf as _subset
from app import manifest as _manifest
from app import measure as _measure
from app.stl_to_3mf import restore_z_from_source as _restore_z_from_source, strip_application_version as _strip_app_version

import app.main as main


@main.app.post(
    "/api/v1/arrange",
    tags=["arrange"],
    summary="Auto-arrange and orient objects in a 3MF (synchronous)",
    description=(
        "Runs OrcaSlicer's plate-packing (`--arrange 1`) and, only if `orient=true`, "
        "auto-orientation (`--orient 1`, which may tilt parts about X/Y) on an existing `.3mf` and returns the rearranged `.3mf` directly "
        "as `application/octet-stream`.\n\n"
        "**Blocking:** waits up to `ARRANGE_TIMEOUT_SECONDS` (default 120 s) for OrcaSlicer "
        "to finish. No job tracking — call completes synchronously.\n\n"
        "**When to use this vs `POST /api/v1/pack`:**\n"
        "- Use `POST /api/v1/arrange` when you already have a `.3mf` with the correct geometry "
        "and want to repack it (e.g., after hand-editing or re-importing).\n"
        "- Use `POST /api/v1/pack` when starting from STL files that need to be combined into "
        "a 3MF first.\n\n"
        "**Settings preservation:** the input 3MF's `Metadata/project_settings.config` is "
        "passed to OrcaSlicer via `--datadir`, which means OrcaSlicer may load its own "
        "machine profiles from the user presets under `/config` (symlinked into Orca's "
        "datadir — see `ORCA_DATADIR`). The `Metadata/model_settings.config` (extruder/slot "
        "assignments) from the input 3MF is preserved in the output.\n\n"
        "All temporary files are cleaned up after the response is sent, including the "
        "stable output copy used to serve the download."
    ),
    responses={
        400: {
            "description": (
                "Bad filename (null bytes, semicolons, or missing Content-Disposition filename), "
                "non-.3mf file uploaded, OrcaSlicer returned non-zero exit code, "
                "or output file missing after OrcaSlicer claims success"
            ),
        },
        408: {"description": "OrcaSlicer arrange timed out — reduce model complexity or increase ARRANGE_TIMEOUT_SECONDS"},
        500: {"description": "Unexpected system error — check server logs"},
    },
    operation_id="v1_auto_arrange_3mf")
async def auto_arrange_3mf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    arrange: Optional[str] = Form(None),
    orient: Optional[str] = Form(None),
):
    arrange = main._form_default_true(arrange)
    # Opt-in: --orient tilts parts about X/Y, changing layer-line direction.
    orient = orient is not None and main._form_default_true(orient)
    # Fix R1: _safe_filename now guards against None before any attribute access
    try:
        safe_name = main._safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=400, detail="Arrange endpoint only supports .3mf files.")

    job_id = str(uuid.uuid4())
    with main._track_inflight_arrange(job_id):
        job_dir = os.path.join(main.ARRANGE_DIR, job_id)
        input_dir = os.path.join(job_dir, "input")
        output_dir = os.path.join(job_dir, "output")

        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        in_file = os.path.join(input_dir, safe_name)
        out_file = os.path.join(output_dir, f"arranged_{safe_name}")

        # Fix R8: blocking file write — run in thread pool
        with open(in_file, "wb") as buffer:
            await asyncio.to_thread(shutil.copyfileobj, file.file, buffer)

        cmd = [
            "xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
            "orcaslicer",
            "--datadir", main.ORCA_DATADIR,
            "--export-3mf", out_file,
        ]

        if arrange:
            cmd.extend(["--arrange", "1"])
        if orient:
            cmd.extend(["--orient", "1"])

        # Orca on Linux segfaults (exit 139) on 3MFs carrying its own version metadata, which
        # includes anything Orca wrote (e.g. a /pack output). Hand it a stripped copy.
        orca_in = in_file
        try:
            stripped = os.path.join(input_dir, f"stripped_{safe_name}")
            await asyncio.to_thread(_strip_app_version, in_file, stripped, False)
            orca_in = stripped
        except ValueError as e:  # decompression-bomb guard
            background_tasks.add_task(main.cleanup_directory, job_dir)
            raise HTTPException(status_code=422, detail=str(e))
        except Exception:
            main.logger.warning("Could not strip version metadata from arrange input", exc_info=True)

        cmd.append(orca_in)
        main.logger.info(f"Running arrange command: {' '.join(cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )

            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=float(main.ARRANGE_TIMEOUT))
            except asyncio.TimeoutError:
                await main._kill_process_group(process)
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=408, detail=f"Slicer arrange execution timed out after {main.ARRANGE_TIMEOUT} seconds.")
            exit_code = process.returncode

            if exit_code != 0 or not os.path.exists(out_file):
                # Fix R4: don't embed raw slicer output (contains internal paths) in HTTP response
                main.logger.error(
                    f"Arrangement failed. Exit code {exit_code}. Log:\n"
                    + (stdout.decode("utf-8", errors="replace") if stdout else "(no output)")
                )
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(
                    status_code=400,
                    detail=f"Slicer auto-arrange process failed (exit code {exit_code}). Check server logs for details.",
                )

            if not orient:
                # Orca re-seats every object at min Z = 0; restore each object's input min Z.
                try:
                    await asyncio.to_thread(_restore_z_from_source, in_file, out_file)
                except Exception:
                    main.logger.exception("Could not restore input Z positions; returning Orca's output as-is")

            stable_out = os.path.join(main.ARRANGE_DIR, f"{job_id}_output.3mf")
            shutil.copy2(out_file, stable_out)

            response = FileResponse(
                path=stable_out,
                filename=f"arranged_{safe_name}",
                media_type="application/octet-stream",
            )
            background_tasks.add_task(main.cleanup_directory, job_dir)
            background_tasks.add_task(main.cleanup_file, stable_out)
            return response

        except HTTPException:
            raise
        except Exception as e:
            background_tasks.add_task(main.cleanup_directory, job_dir)
            main.logger.exception("System exception during arrange operation")
            raise HTTPException(status_code=500, detail=f"System error during arrangement: {str(e)}")


@main.app.post(
    "/api/v1/projects/inspect",
    tags=["arrange"],
    summary="List the objects in an OrcaSlicer/Bambu .3mf",
    description=(
        "Returns each object's `id`, `name`, extruder, per-object override keys, part "
        "subtypes (e.g. `modifier_part`) and current instance count. Use the names/ids in "
        "`POST /api/v1/projects/subset`. Names are not unique in a project; ids are."
    ),
    responses={400: {"description": "Not a readable .3mf"}},
    operation_id="v1_list_3mf_objects")
async def list_3mf_objects(file: UploadFile = File(...)):
    job_dir = os.path.join(main.ARRANGE_DIR, f"objs_{uuid.uuid4()}")
    os.makedirs(job_dir, exist_ok=True)
    try:
        path = os.path.join(job_dir, "in.3mf")
        with open(path, "wb") as buf:
            await asyncio.to_thread(shutil.copyfileobj, file.file, buf)
        try:
            return {"objects": await asyncio.to_thread(_subset.list_objects, path)}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot read 3MF: {e}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def _embed_manifest(path: str, origins: dict[str, list[dict]],
                          origin: Optional[_manifest.Origin] = None) -> None:
    """Embed the BIZ-359 manifest; *origins* = {output object id: [origin per instance]}."""
    def _do():
        m = _manifest.build_manifest(path, origin or _manifest.keyed_origin(origins),
                                     _manifest.requested_from(origins))
        _manifest.embed_manifest(path, m)
    try:
        await asyncio.to_thread(_do)
    except Exception as e:
        main.logger.exception("Plate manifest failed for %s", path)
        raise HTTPException(status_code=500, detail=f"Could not build the plate manifest: {e}")


async def _oracle_arrange_and_transplant(sub_file: str, oracle_in: str, out_file: str,
                                         final_file: str, allow_rotations: bool) -> int:
    """Run OrcaSlicer --arrange on the expanded oracle copy and transplant the layout into
    ``sub_file`` -> ``final_file``. Returns the plate count; raises HTTPException."""
    await main._orca_arrange(oracle_in, out_file, allow_rotations)

    # Orca's own export of the arranged file is lossy (clones objects, drops overrides),
    # so only its layout is used; it is transplanted into the byte-intact subset.
    try:
        plates = await asyncio.to_thread(_subset.transplant_layout, sub_file, out_file, final_file)
    except Exception as e:
        main.logger.exception("Layout transplant failed")
        raise HTTPException(status_code=500, detail=f"Could not apply arranged layout: {e}")
    return plates


async def _orca_arrange(oracle_in: str, out_file: str, allow_rotations: bool) -> None:
    """``orcaslicer --arrange 1 --export-3mf out_file oracle_in``; raises HTTPException."""
    cmd = ["xvfb-run", "-a", "--server-args=-screen 0 1024x768x24", "orcaslicer",
           "--datadir", main.ORCA_DATADIR, "--arrange", "1"]
    if allow_rotations:
        cmd.append("--allow-rotations")
    cmd += ["--export-3mf", out_file, oracle_in]
    main.logger.info("Running subset arrange: %s", " ".join(cmd))
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=float(main.ARRANGE_TIMEOUT))
        except asyncio.TimeoutError:
            await main._kill_process_group(process)
            raise HTTPException(status_code=408, detail=f"Arrange timed out after {main.ARRANGE_TIMEOUT} seconds.")
    except HTTPException:
        raise
    except Exception as e:
        main.logger.exception("System error during subset arrange")
        raise HTTPException(status_code=500, detail=f"System error: {e}")
    if process.returncode != 0 or not os.path.exists(out_file):
        main.logger.error("Subset arrange failed (exit %s):\n%s", process.returncode,
                     stdout.decode("utf-8", errors="replace") if stdout else "(no output)")
        raise HTTPException(status_code=400, detail=f"Slicer arrange failed (exit {process.returncode}). Check server logs.")


@main.app.post(
    "/api/v1/projects/previews",
    tags=["arrange"],
    summary="List a .3mf's objects with plate names and a preview image of each object",
    description=(
        "For each object: `id`, `name`, `extruder`, `plates` (`[{plate, name, instances}]` - "
        "`name` is the plate's name in the project, or `Plate N` when unnamed) and `preview`.\n\n"
        "`preview` is a rendering of **the object itself** (not the plate): a 3/4-view PNG, "
        "transparent background, tinted with the object's filament colour, modifier/negative "
        "parts omitted; `{mime, width, height, data_base64}`, or `null` when the object has "
        "nothing printable. Use the `id`s with `/api/v1/projects/subset` or `/api/v1/projects/merge`.\n\n"
        "`size` is the square image edge in px (32-1024, default 256). Set `images=false` for "
        "names/plates only (fast). Rendering is CPU-bound: roughly 0.5 s per object for typical "
        "models, so a 20-object project takes ~10 s."
    ),
    responses={
        400: {"description": "Not a readable .3mf"},
        422: {"description": "size out of range"},
    },
    operation_id="v1_preview_3mf_objects")
async def preview_3mf_objects(
    file: UploadFile = File(...),
    size: int = Form(256),
    images: Optional[str] = Form(None),
):
    images = main._form_default_true(images)
    if not 32 <= size <= 1024:
        raise HTTPException(status_code=422, detail="size must be between 32 and 1024.")
    job_dir = os.path.join(main.ARRANGE_DIR, f"prev_{uuid.uuid4()}")
    os.makedirs(job_dir, exist_ok=True)
    try:
        path = os.path.join(job_dir, "in.3mf")
        with open(path, "wb") as buf:
            await asyncio.to_thread(shutil.copyfileobj, file.file, buf)
        try:
            objs = await asyncio.to_thread(_preview.object_previews, path, size, images)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot read 3MF: {e}")
        return {"objects": objs}
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@main.app.post(
    "/api/v1/projects/subset",
    tags=["arrange"],
    summary="Keep only chosen objects (x quantity) from a .3mf and arrange them on the fewest plates",
    description=(
        "Takes a `.3mf`, a `selection` (JSON list of `{\"name\"|\"id\"|\"plate\": ..., \"qty\": n}`; "
        "`plate` selects everything on that source plate) and "
        "optionally a `machine_uuid`, and returns a `.3mf` containing only those objects, "
        "`qty` copies each, arranged by OrcaSlicer across as many plates as needed.\n\n"
        "**Preservation:** the returned file is the source project with only the chosen "
        "objects kept; meshes and `model_settings.config` are copied byte-for-byte, so "
        "per-object/per-part overrides, modifier parts, height-range modifiers and painted "
        "data (colour, supports, seam, fuzzy skin) survive. OrcaSlicer is used only to "
        "compute the layout (plate membership + instance transforms), which is written "
        "into that file; OrcaSlicer's own re-export is not returned because it splits "
        "cross-plate instances into clone objects that lose name/overrides/extruder.\n\n"
        "**Printer:** when `machine_uuid` is given, the bed/printer keys of the project "
        "are replaced from that machine preset and the embedded process preset's "
        "printer-compatibility list is cleared. Process/filament settings are otherwise "
        "left as in the source project.\n\n"
        "**Plates:** OrcaSlicer's multi-plate arrange is used as-is; response header "
        "`X-Plate-Count` reports the result; `X-Warnings` lists any object not resting on "
        "the bed (empty when fine). Selector names matching several objects are "
        "rejected (422) — use `id`.\n\n"
        "**Manifest:** embeds `Metadata/laminus_manifest.json`: per plate, which `selection` "
        "entry and source object each instance came from (feature `pack.manifest`).\n\n"
        "Blocking; limited by `ARRANGE_TIMEOUT_SECONDS`."
    ),
    responses={
        400: {"description": "Unreadable 3MF or Orca arrange failed"},
        408: {"description": "Arrange timed out"},
        422: {"description": "Bad selection, unknown/ambiguous object, unknown machine_uuid"},
        503: {"description": "machine_uuid given but catalog not ready"},
    },
    operation_id="v1_arrange_subset")
async def arrange_subset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    selection: str = Form(..., description='JSON: [{"name": "Lid", "qty": 4}, {"id": 12, "qty": 1}]'),
    machine_uuid: Optional[str] = Form(None),
    process_uuid: Optional[str] = Form(None, description="Override the project's process preset (requires machine_uuid)"),
    filament_uuids: Optional[str] = Form(None, description='JSON array of filament preset UUIDs, one per slot (requires machine_uuid)'),
    allow_rotations: bool = Form(False),
):
    try:
        sel = json.loads(selection)
        if not isinstance(sel, list) or not all(isinstance(x, dict) for x in sel):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="selection must be a JSON list of objects.")
    try:
        safe_name = main._safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=400, detail="Only .3mf files are supported.")

    target = await main._resolve_retarget(machine_uuid, process_uuid, filament_uuids)

    job_id = str(uuid.uuid4())
    with main._track_inflight_arrange(job_id):
        job_dir = os.path.join(main.ARRANGE_DIR, f"subset_{job_id}")
        os.makedirs(job_dir, exist_ok=True)
        background_tasks.add_task(main.cleanup_directory, job_dir)
        in_file = os.path.join(job_dir, "in.3mf")
        sub_file = os.path.join(job_dir, "subset.3mf")
        oracle_in = os.path.join(job_dir, "oracle_in.3mf")
        out_file = os.path.join(job_dir, "arranged.3mf")
        final_file = os.path.join(job_dir, "final.3mf")
        with open(in_file, "wb") as buf:
            await asyncio.to_thread(shutil.copyfileobj, file.file, buf)

        origins: dict[int, list[int]] = {}

        def _build():
            want = _subset.resolve_selection(_subset.list_objects(in_file), sel,
                                             _subset.plate_members(in_file), origins)
            summary = _subset.build_subset(in_file, sub_file, want, **target.kwargs)
            summary["remap"] += target.compat_warnings(sub_file)
            _subset.expand_instances(sub_file, oracle_in)
            return summary

        try:
            summary = await asyncio.to_thread(_build)
        except _subset.SubsetError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot process 3MF: {e}")

        plates = await main._oracle_arrange_and_transplant(
            sub_file, oracle_in, out_file, final_file, allow_rotations)
        await main._embed_manifest(final_file, {
            str(oid): [{"item": i, "source": safe_name, "part": str(oid)} for i in items]
            for oid, items in origins.items()})
        stable_out = os.path.join(main.ARRANGE_DIR, f"{job_id}_subset.3mf")
        shutil.copy2(final_file, stable_out)
        background_tasks.add_task(main.cleanup_file, stable_out)
        warn = " | ".join(await asyncio.to_thread(_preview.bed_clearance_warnings, final_file))
        headers = {"X-Instance-Count": str(summary["instances"]), "X-Plate-Count": str(plates),
                   "X-Warnings": warn.encode("ascii", "replace").decode(),
                   "X-Remap-Warnings": " | ".join(summary["remap"]).encode("ascii", "replace").decode()}
        return FileResponse(path=stable_out, filename=f"subset_{safe_name}",
                            media_type="application/octet-stream", headers=headers)


class _Retarget:
    """Resolved machine/process/filament presets for retargeting a subset/merged 3MF."""

    def __init__(self, machine=None, printer_cfg=None, process_cfg=None, filament_cfgs=None):
        self.machine, self.printer_cfg = machine, printer_cfg
        self.process_cfg, self.filament_cfgs = process_cfg, filament_cfgs

    @property
    def kwargs(self) -> dict:
        return {"printer_cfg": self.printer_cfg, "process_cfg": self.process_cfg,
                "filament_cfgs": self.filament_cfgs}

    def compat_warnings(self, path: str) -> list[str]:
        """Warn when an embedded (not overridden) preset lists printers excluding the target."""
        if self.machine is None or main.catalog is None:
            return []
        with _subset.open_zip(path) as z:
            cfg = json.loads(z.read(_subset.PROJECT_SETTINGS))
        wanted = []
        if self.process_cfg is None:
            wanted.append(("process", cfg.get("print_settings_id"), "process_uuid"))
        if self.filament_cfgs is None:
            ids = cfg.get("filament_settings_id") or []
            wanted += [("filament", n, "filament_uuids") for n in (ids if isinstance(ids, list) else [ids])]
        out = []
        for kind, name, param in wanted:
            for e in main.catalog._catalog.get(kind, []):
                compat = e.get("compatible_printers") or []
                if e["name"] == name and compat and self.machine["name"] not in compat:
                    out.append(f"{kind} preset '{name}' is not compatible with '{self.machine['name']}'; "
                               f"pass {param} to remap")
                    break
        return out


async def _resolve_retarget(machine_uuid, process_uuid, filament_uuids) -> _Retarget:
    if not machine_uuid:
        if process_uuid or filament_uuids:
            raise HTTPException(status_code=422, detail="process_uuid/filament_uuids require machine_uuid.")
        return main._Retarget()
    if main.catalog is None:
        raise HTTPException(status_code=503, detail="Profile catalog not yet ready.")

    async def _load(uid, kind):
        entry = main.catalog.get_by_uuid(uid)
        if entry is None or entry.get("type") != kind:
            raise HTTPException(status_code=422, detail=f"{kind.capitalize()} UUID '{uid}' not found.")
        return entry, await asyncio.to_thread(main.catalog.resolved, entry["uuid"])

    machine, printer_cfg = await _load(machine_uuid, "machine")
    process_cfg = (await _load(process_uuid, "process"))[1] if process_uuid else None
    filament_cfgs = None
    if filament_uuids:
        try:
            uids = json.loads(filament_uuids)
            if not isinstance(uids, list) or not uids or not all(isinstance(u, str) for u in uids):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=422, detail="filament_uuids must be a non-empty JSON array of strings.")
        filament_cfgs = [(await _load(u, "filament"))[1] for u in uids]
    return main._Retarget(machine, printer_cfg, process_cfg, filament_cfgs)


@main.app.post(
    "/api/v1/projects/merge",
    tags=["arrange"],
    summary="Combine selections from several .3mf files into one .3mf, arranged on the fewest plates",
    description=(
        "Upload several `.3mf` files as repeated `files` parts and a `selection` JSON list whose "
        "entries each name their source with `file` (the uploaded filename, or the 0-based upload "
        "index) plus one of `id` / `name` / `plate` and a `qty`:\n\n"
        "`[{\"file\": \"hex.3mf\", \"plate\": 1, \"qty\": 2}, {\"file\": 1, \"id\": 8, \"qty\": 1}]`\n\n"
        "`plate` selects every object on that plate of the source (x its count there x `qty`). "
        "Returns one `.3mf`. Per-object/part overrides, modifiers and painted data travel with "
        "each object. **Project-wide settings (process, filament, printer, thumbnails) come from "
        "the first uploaded file** (the base); differences in the other sources are listed in "
        "the `X-Merge-Warnings` header, which also reports any object that ends up not resting on the bed (floating/sunk). A non-base object using a filament slot the base does not "
        "have is rejected (422). `machine_uuid` retargets the bed/printer as in `/api/v1/projects/subset`.\n\n"
        "Embeds `Metadata/laminus_manifest.json`: per plate, which `selection` entry and source "
        "object each instance came from (feature `pack.manifest`).\n\n"
        "Headers: `X-Plate-Count`, `X-Instance-Count`, `X-Merge-Warnings`. Blocking; limited by "
        "`ARRANGE_TIMEOUT_SECONDS`."
    ),
    responses={
        400: {"description": "Unreadable 3MF or Orca arrange failed"},
        408: {"description": "Arrange timed out"},
        422: {"description": "Bad selection, unknown/ambiguous object, unknown file, unknown machine_uuid, incompatible sources"},
        503: {"description": "machine_uuid given but catalog not ready"},
    },
    operation_id="v1_arrange_merge")
async def arrange_merge(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    selection: str = Form(..., description='JSON: [{"file": "a.3mf", "plate": 1, "qty": 2}, {"file": 1, "id": 8, "qty": 1}]'),
    machine_uuid: Optional[str] = Form(None),
    process_uuid: Optional[str] = Form(None, description="Override the project's process preset (requires machine_uuid)"),
    filament_uuids: Optional[str] = Form(None, description='JSON array of filament preset UUIDs, one per slot (requires machine_uuid)'),
    allow_rotations: bool = Form(False),
):
    try:
        sel = json.loads(selection)
        if not isinstance(sel, list) or not all(isinstance(x, dict) and "file" in x for x in sel):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail='selection must be a JSON list of objects, each with a "file".')
    names: list[str] = []
    for f in files:
        try:
            n = main._safe_filename(f.filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not n.lower().endswith(".3mf"):
            raise HTTPException(status_code=400, detail="Only .3mf files are supported.")
        names.append(n)
    # group selectors per source file
    per_file: dict[int, list[dict]] = {}
    per_file_idx: dict[int, list[int]] = {}    # global selection index of each per-file entry
    for sel_idx, item in enumerate(sel):
        ref = item["file"]
        if isinstance(ref, int):
            if not 0 <= ref < len(files):
                raise HTTPException(status_code=422, detail=f"selection file index {ref} out of range.")
            idx = ref
        else:
            hits = [i for i, n in enumerate(names) if n == os.path.basename(str(ref))]
            if len(hits) != 1:
                raise HTTPException(status_code=422, detail=(
                    f"selection file {ref!r} matches {len(hits)} uploads; use the 0-based index."))
            idx = hits[0]
        per_file.setdefault(idx, []).append({k: v for k, v in item.items() if k != "file"})
        per_file_idx.setdefault(idx, []).append(sel_idx)
    order = sorted(per_file, key=lambda i: (i != 0, i))  # base (first upload) first if selected
    if 0 not in per_file:
        raise HTTPException(status_code=422, detail=(
            "The first uploaded file is the base for project settings and must have at least one selection."))

    target = await main._resolve_retarget(machine_uuid, process_uuid, filament_uuids)

    job_id = str(uuid.uuid4())
    with main._track_inflight_arrange(job_id):
        job_dir = os.path.join(main.ARRANGE_DIR, f"merge_{job_id}")
        os.makedirs(job_dir, exist_ok=True)
        background_tasks.add_task(main.cleanup_directory, job_dir)
        for i in order:
            with open(os.path.join(job_dir, f"in_{i}.3mf"), "wb") as buf:
                await asyncio.to_thread(shutil.copyfileobj, files[i].file, buf)
        merged = os.path.join(job_dir, "merged.3mf")
        oracle_in = os.path.join(job_dir, "oracle_in.3mf")
        out_file = os.path.join(job_dir, "arranged.3mf")
        final_file = os.path.join(job_dir, "final.3mf")

        remap: list[str] = []
        merged_origins: dict[str, list[dict]] = {}

        def _build():
            subs, file_origins = [], []
            for i in order:
                src = os.path.join(job_dir, f"in_{i}.3mf")
                origins: dict[int, list[int]] = {}
                try:
                    want = _subset.resolve_selection(_subset.list_objects(src), per_file[i],
                                                     _subset.plate_members(src), origins,
                                                     per_file_idx[i])
                except _subset.SubsetError as e:
                    raise _subset.SubsetError(f"{names[i]}: {e}")
                dst = os.path.join(job_dir, f"sub_{i}.3mf")
                r = _subset.build_subset(src, dst, want, **target.kwargs)
                remap.extend(f"{names[i]}: {w}" for w in r["remap"] + target.compat_warnings(dst))
                subs.append(dst)
                file_origins.append(origins)
            summary = _subset.merge_subsets(subs, merged, labels=[names[i] for i in order])
            for i, origins, id_map in zip(order, file_origins, summary["id_maps"]):
                for oid, items in origins.items():
                    merged_origins[id_map[str(oid)]] = [
                        {"item": s, "source": names[i], "part": str(oid)} for s in items]
            _subset.expand_instances(merged, oracle_in)
            return summary

        try:
            summary = await asyncio.to_thread(_build)
        except _subset.SubsetError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Cannot process 3MF: {e}")

        plates = await main._oracle_arrange_and_transplant(
            merged, oracle_in, out_file, final_file, allow_rotations)
        await main._embed_manifest(final_file, merged_origins)
        stable_out = os.path.join(main.ARRANGE_DIR, f"{job_id}_merge.3mf")
        shutil.copy2(final_file, stable_out)
        background_tasks.add_task(main.cleanup_file, stable_out)
        warnings = summary["warnings"] + await asyncio.to_thread(
            _preview.bed_clearance_warnings, final_file)
        warn = " | ".join(warnings).encode("ascii", "replace").decode()
        headers = {"X-Instance-Count": str(summary["instances"]), "X-Plate-Count": str(plates),
                   "X-Merge-Warnings": warn,
                   "X-Remap-Warnings": " | ".join(remap).encode("ascii", "replace").decode()}
        return FileResponse(path=stable_out, filename="merged.3mf",
                            media_type="application/octet-stream", headers=headers)


@main.app.post(
    "/api/v1/projects/measure",
    tags=["arrange"],
    summary="Bounding-box sizes of 3MF objects / plates, or of an STL",
    description=(
        "Multipart `file` (`.3mf` or `.stl`) and optional `selection`, a JSON list of "
        "`{\"id\": n}` / `{\"name\": s}` (one object) or `{\"plate\": n}` (one plate); `[]` or "
        "omitted measures the whole file.\n\n"
        "**200:** `{\"items\": [{\"size_mm\": [x, y, z]}, ...]}`, one per selection entry in order "
        "(one for the whole file). Sizes are the axis-aligned bounding box of printed parts "
        "(modifier/negative/blocker/enforcer parts ignored), after the build transform:\n"
        "- object: its first instance as placed (a rotated object reports its placed size);\n"
        "- plate: every instance on that plate as authored;\n"
        "- whole 3MF: every build item; a multi-plate project needs `plate` selectors;\n"
        "- STL: mesh extents (no selection).\n\n"
        "**422** (`unprocessable`): unreadable file, bad selection, unknown/ambiguous object, "
        "missing or empty plate."
    ),
    responses={422: {"description": "Unreadable file or bad selection"}},
    operation_id="v1_measure")
async def measure_project(
    file: UploadFile = File(...),
    selection: Optional[str] = Form(None, description='JSON: [{"id": 8}, {"plate": 2}]; omit for the whole file'),
):
    sel = None
    if selection not in (None, ""):
        try:
            sel = json.loads(selection)
            if not isinstance(sel, list) or not all(isinstance(x, dict) for x in sel):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=422, detail="selection must be a JSON list of objects.")
    try:
        safe_name = main._safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    stl = safe_name.lower().endswith(".stl")
    if not stl and not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=422, detail="Only .3mf and .stl files are supported.")
    job_dir = os.path.join(main.ARRANGE_DIR, f"measure_{uuid.uuid4()}")
    os.makedirs(job_dir, exist_ok=True)
    try:
        path = os.path.join(job_dir, "in.stl" if stl else "in.3mf")
        with open(path, "wb") as buf:
            await asyncio.to_thread(shutil.copyfileobj, file.file, buf)
        try:
            sizes = await asyncio.to_thread(_measure.measure, path, stl, sel)
        except _measure.MeasureError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Cannot read {'STL' if stl else '3MF'}: {e}")
        return {"items": [{"size_mm": s} for s in sizes]}
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
