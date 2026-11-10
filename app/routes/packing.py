"""Packing routes: /pack (STLs onto plates), /pack/next and /pack/mixed, and the selection parsing they share.

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
from app.project_config_builder import build_project_settings
from app import preview_3mf as _preview
from app import subset_3mf as _subset
from app import manifest as _manifest
from app import pack_mixed as _pack_mixed
from app import pack_next as _pack_next
from app import plate_extract as _plate_extract
from app.stl_to_3mf import stl_min_z as _stl_min_z, restore_build_item_z as _restore_build_item_z
from app import api_v1 as _api_v1  # noqa: E402  (installs /api/v1 once every legacy route exists)

import app.main as main


def _build_bed_template_bytes(printable_area: list, printable_height: float) -> bytes:
    """Minimal 3MF carrying only bed dimensions for use as a pack template.

    Only printable_area and printable_height are embedded; a full project config
    would trigger OrcaSlicer's machine lookup and override the bed size.
    """
    import io, zipfile as _zf
    ct = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0"'
        ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        '</Relationships>'
    )
    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w", compression=_zf.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("Metadata/project_settings.config",
                    json.dumps({"printable_area": printable_area,
                                "printable_height": printable_height}, ensure_ascii=False))
    return buf.getvalue()


def _items_error(errors: list[dict]):
    summary = "Invalid items: " + "; ".join(e["msg"] for e in errors)
    return _api_v1.DetailedHTTPException(422, summary, "validation_error", errors)


def _parse_pack_items(raw: str, filenames: list[str]) -> list[int]:
    """Validate the /api/v1/pack ``items`` field; return copy counts in upload order.

    422s carry per-problem ``details`` on v1 (legacy sees only the summary string).
    """
    base = ["body", "items"]
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, list):
        raise main._items_error([{"loc": base, "msg": "must be a JSON array of {file, quantity} objects",
                             "type": "type_error"}])

    errors: list[dict] = []
    dupes = sorted({n for n in filenames if filenames.count(n) > 1})
    for n in dupes:
        errors.append({"loc": ["body", "files"], "type": "value_error", "input": n,
                       "msg": f"duplicate filename '{n}'; filenames must be unique when items is used"})

    qty_by_name: dict[str, int] = {}
    for i, entry in enumerate(parsed):
        loc = base + [i]
        if not isinstance(entry, dict):
            errors.append({"loc": loc, "msg": "must be an object", "type": "type_error", "input": entry})
            continue
        name, qty = entry.get("file"), entry.get("quantity")
        ok = True
        if not isinstance(name, str) or name not in filenames:
            errors.append({"loc": loc + ["file"], "type": "value_error", "input": name,
                           "msg": "must be the filename of an uploaded file"})
            ok = False
        elif name in qty_by_name:
            errors.append({"loc": loc + ["file"], "type": "value_error", "input": name,
                           "msg": f"'{name}' listed more than once"})
            ok = False
        if isinstance(qty, bool) or not isinstance(qty, int):
            errors.append({"loc": loc + ["quantity"], "type": "type_error", "input": qty,
                           "msg": "must be an integer"})
            ok = False
        elif qty < 1:
            errors.append({"loc": loc + ["quantity"], "type": "value_error", "input": qty,
                           "msg": "must be >= 1"})
            ok = False
        if ok:
            qty_by_name[name] = qty

    if not dupes:
        for n in filenames:
            if n not in qty_by_name and not any(e.get("input") == n for e in errors):
                errors.append({"loc": base, "type": "value_error", "input": n,
                               "msg": f"no entry for uploaded file '{n}'"})
    if not errors:
        total = sum(qty_by_name.values())
        if total > main.PACK_MAX_INSTANCES:
            errors.append({"loc": base, "type": "value_error",
                           "msg": f"total instances {total} exceeds max {main.PACK_MAX_INSTANCES}",
                           "ctx": {"total": total, "max": main.PACK_MAX_INSTANCES}})
    if errors:
        raise main._items_error(errors)
    return [qty_by_name[n] for n in filenames]


@main.app.post(
    "/api/v1/pack",
    tags=["arrange"],
    summary="Pack N STLs onto as many plates as needed (synchronous)",
    description=(
        "Distributes STL geometry across build plates via OrcaSlicer `--arrange 1` (no auto-orient). "
        "Returns the resulting multi-plate `.3mf` directly as `application/octet-stream`. "
        "**Blocks for up to 120 seconds.**\n\n"
        "## Bed / settings source (mutually exclusive — supply exactly one)\n\n"
        "**1. Template file** (`template` field)\n"
        "Upload a `.3mf` whose `Metadata/project_settings.config` carries the printer and "
        "bed configuration. OrcaSlicer reads bed dimensions and settings from that config. "
        "Use this when you already have a known-good 3MF template from a previous session.\n\n"
        "**2. Profile UUIDs** (`machine_uuid` + `process_uuid` + `filament_uuids`)\n"
        "The container resolves profiles from its catalog, builds the settings internally, "
        "and constructs a minimal bed-only template (only `printable_area` and "
        "`printable_height` are embedded — no full machine config that could override the "
        "bed size). Templates for a given UUID combination are cached in memory, so repeated "
        "calls with the same profiles are fast. This mode requires `catalog_loaded: true`.\n\n"
        "**3. Machine UUID alone** (`machine_uuid`, no `process_uuid`/`filament_uuids`)\n"
        "Shortcut for geometry mode: the bed (`printable_area`, `printable_height`) is read from "
        "that machine's catalog profile, so callers need not look up bed_x/bed_y/bed_z first. "
        "If `bed_x`/`bed_y`/`bed_z` are all given too, they win. Requires `catalog_loaded: true`.\n\n"
        "**4. Explicit bed dimensions** (`bed_x` + `bed_y` + `bed_z`, all in mm)\n"
        "Laminus constructs a minimal bed template from the supplied dimensions. "
        "`bed_z` is the printable height (not the physical Z travel). "
        "No profile catalog or template file needed — useful when you want geometry-only "
        "packing without print settings.\n\n"
        "## STL inputs\n\n"
        "- `files` — one or more `.stl` files (max 50 per request)\n"
        "- Filenames must not contain null bytes, newlines, or semicolons\n"
        "- Duplicate filenames in the same batch are deduplicated with a numeric suffix\n"
        "- `items` (optional) — JSON array of `{\"file\": <uploaded filename>, \"quantity\": <int >= 1>}`, "
        "e.g. `[{\"file\":\"a.stl\",\"quantity\":3},{\"file\":\"b.stl\",\"quantity\":1}]`. Each STL "
        "yields that many arranged copies sharing one mesh in the 3MF. Omitted = 1 each. When "
        "`items` is given, uploaded filenames must be unique and every upload needs exactly one "
        "entry; anything else → 422. The 50 cap applies to unique files; total copies are "
        f"capped at {main.PACK_MAX_INSTANCES}. On `/api/v1/pack` a 422 lists each problem in "
        "`error.details` with a `loc` such as `[\"body\",\"items\",1,\"quantity\"]`.\n\n"
        "## Notes\n\n"
        "- Parts are never tilted about X/Y (no auto-orient), so layer-line direction is "
        "preserved. Each instance keeps its input STL's min Z (models intentionally above or "
        "below the plate stay there); the arranger may rotate about Z.\n"
        "- The packed `.3mf` preserves `Metadata/project_settings.config` from the template "
        "but discards any geometry or model_settings from it.\n"
        "- All temporary files are cleaned up after the response is sent.\n"
        "- Embeds `Metadata/laminus_manifest.json`: per plate, which `items` entry each "
        "instance came from (feature `pack.manifest`; see docs/laminus-api-for-agents.md)."
    ),
    responses={
        400: {
            "description": (
                "Bad filename in STL or template upload, OrcaSlicer arrange process failed "
                "(non-zero exit or missing output file), or failed to build the combined 3MF"
            ),
        },
        408: {"description": "Pack operation timed out after 120 seconds"},
        422: {
            "description": (
                "No STL files provided, more than 50 STLs, non-.stl file in files list, "
                "non-.3mf template, bed dimensions ≤ 0, invalid filament_uuids JSON, "
                "invalid `items` (bad JSON/shape, unknown/duplicate/missing file, quantity < 1) "
                "or total copies over the cap, "
                "UUID not found in catalog, or no settings source provided"
            ),
        },
        500: {"description": "Unexpected system error — check server logs"},
        503: {"description": "Profile catalog not yet ready (UUID mode only)"},
    },
    operation_id="v1_pack_stls")
async def pack_stls(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    template: Optional[UploadFile] = File(None, description=".3mf with Metadata/project_settings.config"),
    machine_uuid: Optional[str] = Form(None),
    process_uuid: Optional[str] = Form(None),
    filament_uuids: Optional[str] = Form(None, description='JSON array, e.g. ["uuid1"]'),
    bed_x: Optional[float] = Form(None, description="Bed X dimension in mm (alternative to template / UUIDs)"),
    bed_y: Optional[float] = Form(None, description="Bed Y dimension in mm"),
    bed_z: Optional[float] = Form(None, description="Bed Z printable height in mm"),
    items: Optional[str] = Form(
        None,
        description=(
            'JSON array pairing each uploaded file with a copy count, e.g. '
            '`[{"file": "a.stl", "quantity": 3}, {"file": "b.stl", "quantity": 1}]`. '
            f"Omitted = 1 copy each. Total copies capped at {main.PACK_MAX_INSTANCES}."
        ),
    ),
):
    if not files:
        raise HTTPException(status_code=422, detail="At least one STL file is required.")
    if len(files) > 50:
        raise HTTPException(status_code=422, detail="Maximum 50 STL files per request.")

    qty_list: Optional[list[int]] = None
    if items is not None:
        qty_list = main._parse_pack_items(items, [f.filename or "" for f in files])

    # Validate STL filenames, deduplicating repeats within the batch with a numeric
    # suffix so two uploads named e.g. "bracket.stl" don't overwrite one another on disk.
    safe_names: list[str] = []
    seen_counts: dict[str, int] = {}
    for f in files:
        try:
            name = main._safe_filename(f.filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not name.lower().endswith(".stl"):
            raise HTTPException(status_code=422, detail=f"Only .stl files accepted; got: {name}")
        count = seen_counts.get(name, 0)
        seen_counts[name] = count + 1
        if count:
            stem, ext = os.path.splitext(name)
            name = f"{stem}_{count}{ext}"
        safe_names.append(name)

    job_id = str(uuid.uuid4())
    with main._track_inflight_arrange(job_id):
        job_dir = os.path.join(main.ARRANGE_DIR, f"pack_{job_id}")
        input_dir = os.path.join(job_dir, "input")
        output_dir = os.path.join(job_dir, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        # --- Resolve template ---
        if template is not None:
            # Caller-supplied template file
            try:
                template_name = main._safe_filename(template.filename)
            except ValueError as e:
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=400, detail=str(e))
            if not template_name.lower().endswith(".3mf"):
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=422, detail="template must be a .3mf file.")
            template_path = os.path.join(input_dir, template_name)
            with open(template_path, "wb") as buf:
                await asyncio.to_thread(shutil.copyfileobj, template.file, buf)

        elif machine_uuid and process_uuid and filament_uuids:
            # UUID-based: resolve profiles from catalog and build template (cached)
            if main.catalog is None:
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=503, detail="Profile catalog not yet ready.")
            try:
                fil_uuid_list: list[str] = json.loads(filament_uuids)
                if not isinstance(fil_uuid_list, list) or not fil_uuid_list:
                    raise ValueError
            except (ValueError, TypeError):
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=422, detail="filament_uuids must be a non-empty JSON array.")

            cache_key = f"{machine_uuid}|{process_uuid}|{','.join(sorted(fil_uuid_list))}"
            cached_bytes = main._template_cache.get(cache_key)

            if cached_bytes is None:
                machine_entry = main.catalog.get_by_uuid(machine_uuid)
                if machine_entry is None or machine_entry.get("type") != "machine":
                    background_tasks.add_task(main.cleanup_directory, job_dir)
                    raise HTTPException(status_code=422, detail=f"Machine UUID '{machine_uuid}' not found.")
                proc_entry = main.catalog.get_by_uuid(process_uuid)
                if proc_entry is None or proc_entry.get("type") != "process":
                    background_tasks.add_task(main.cleanup_directory, job_dir)
                    raise HTTPException(status_code=422, detail=f"Process UUID '{process_uuid}' not found.")
                fil_entries = []
                for fuid in fil_uuid_list:
                    fe = main.catalog.get_by_uuid(fuid)
                    if fe is None or fe.get("type") != "filament":
                        background_tasks.add_task(main.cleanup_directory, job_dir)
                        raise HTTPException(status_code=422, detail=f"Filament UUID '{fuid}' not found.")
                    fil_entries.append(fe)

                project_cfg = await asyncio.to_thread(
                    build_project_settings,
                    main.catalog.resolved(machine_entry["uuid"]),
                    main.catalog.resolved(proc_entry["uuid"]),
                    [main.catalog.resolved(fe["uuid"]) for fe in fil_entries],
                )

                printable_area = project_cfg.get("printable_area")
                printable_height = project_cfg.get("printable_height")
                if not printable_area or not printable_height:
                    background_tasks.add_task(main.cleanup_directory, job_dir)
                    raise HTTPException(
                        status_code=422,
                        detail=f"Machine UUID '{machine_uuid}' is missing bed dimensions (printable_area/printable_height).",
                    )

                cached_bytes = await asyncio.to_thread(
                    main._build_bed_template_bytes,
                    printable_area,
                    printable_height,
                )
                main._template_cache[cache_key] = cached_bytes
                main.logger.info("Template cached for key %s (%d bytes)", cache_key[:40], len(cached_bytes))
            else:
                main.logger.debug("Template cache hit for key %s", cache_key[:40])

            template_path = os.path.join(input_dir, "settings_template.3mf")
            with open(template_path, "wb") as buf:
                buf.write(cached_bytes)

        elif bed_x is not None and bed_y is not None and bed_z is not None:
            if bed_x <= 0 or bed_y <= 0 or bed_z <= 0:
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=422, detail="bed_x, bed_y, and bed_z must all be positive.")
            area = ["0x0", f"{bed_x}x0", f"{bed_x}x{bed_y}", f"0x{bed_y}"]
            template_path = os.path.join(input_dir, "settings_template.3mf")
            with open(template_path, "wb") as buf:
                buf.write(main._build_bed_template_bytes(area, bed_z))

        elif machine_uuid and not process_uuid and not filament_uuids:
            # machine_uuid alone: shortcut for geometry mode using that machine's bed
            if main.catalog is None:
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=503, detail="Profile catalog not yet ready.")
            cache_key = f"{machine_uuid}|bed"
            cached_bytes = main._template_cache.get(cache_key)
            if cached_bytes is None:
                machine_entry = main.catalog.get_by_uuid(machine_uuid)
                if machine_entry is None or machine_entry.get("type") != "machine":
                    background_tasks.add_task(main.cleanup_directory, job_dir)
                    raise HTTPException(status_code=422, detail=f"Machine UUID '{machine_uuid}' not found.")
                machine_cfg = await asyncio.to_thread(main.catalog.resolved, machine_entry["uuid"])
                printable_area = machine_cfg.get("printable_area")
                printable_height = machine_cfg.get("printable_height")
                if not printable_area or not printable_height:
                    background_tasks.add_task(main.cleanup_directory, job_dir)
                    raise HTTPException(
                        status_code=422,
                        detail=f"Machine UUID '{machine_uuid}' is missing bed dimensions (printable_area/printable_height).",
                    )
                cached_bytes = await asyncio.to_thread(main._build_bed_template_bytes, printable_area, printable_height)
                main._template_cache[cache_key] = cached_bytes
            template_path = os.path.join(input_dir, "settings_template.3mf")
            with open(template_path, "wb") as buf:
                buf.write(cached_bytes)

        else:
            background_tasks.add_task(main.cleanup_directory, job_dir)
            raise HTTPException(
                status_code=422,
                detail="Provide 'template' (a .3mf file) OR 'machine_uuid' (alone, or with 'process_uuid'/'filament_uuids') OR 'bed_x'/'bed_y'/'bed_z'.",
            )

        # Write uploaded STLs
        stl_paths: list[str] = []
        for f, name in zip(files, safe_names):
            dest = os.path.join(input_dir, name)
            with open(dest, "wb") as buf:
                await asyncio.to_thread(shutil.copyfileobj, f.file, buf)
            stl_paths.append(dest)

        # Inject STL geometry into the template (preserves project_settings.config)
        combined_3mf = os.path.join(input_dir, "combined.3mf")
        try:
            await asyncio.to_thread(main._inject_stls_into_3mf, template_path, stl_paths, combined_3mf, qty_list)
        except ValueError as e:
            background_tasks.add_task(main.cleanup_directory, job_dir)
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            background_tasks.add_task(main.cleanup_directory, job_dir)
            raise HTTPException(status_code=400, detail=f"Failed to build combined 3MF: {e}")

        out_file = os.path.join(output_dir, "packed.3mf")
        cmd = [
            "xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
            "orcaslicer",
            "--datadir", main.ORCA_DATADIR,
            "--arrange", "1",
            "--orient", "0",  # --orient tilts parts about X/Y, changing layer-line direction
            "--export-3mf", out_file,
            combined_3mf,
        ]
        main.logger.info("Running pack command: %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=120.0)
            except asyncio.TimeoutError:
                await main._kill_process_group(process)
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(status_code=408, detail="Pack operation timed out after 120 seconds.")
            exit_code = process.returncode

            if exit_code != 0 or not os.path.exists(out_file):
                main.logger.error(
                    "Pack failed. Exit %d. Log:\n%s",
                    exit_code,
                    stdout.decode("utf-8", errors="replace") if stdout else "(no output)",
                )
                background_tasks.add_task(main.cleanup_directory, job_dir)
                raise HTTPException(
                    status_code=400,
                    detail=f"Slicer pack failed (exit {exit_code}). Check server logs.",
                )

            # Orca re-seats every part at min Z = 0; put each back at its input STL's min Z.
            min_zs = await asyncio.to_thread(lambda: [_stl_min_z(p) for p in stl_paths])
            if any(min_zs):
                per_instance = [z for z, q in zip(min_zs, qty_list or [1] * len(min_zs)) for _ in range(q)]
                await asyncio.to_thread(_restore_build_item_z, out_file, per_instance)

            # BIZ-359: per-plate manifest. `item` = index in `items` (upload order without it).
            item_of = {e["file"]: i for i, e in enumerate(json.loads(items))} if items is not None else {}
            per_input = [{"item": item_of.get(f.filename or "", u), "source": f.filename or name,
                          "part": f.filename or name}
                         for u, (f, name, q) in enumerate(zip(files, safe_names, qty_list or [1] * len(files)))
                         for _ in range(q)]
            await main._embed_manifest(out_file, {"pack": per_input},
                                  await asyncio.to_thread(_manifest.pack_origin, out_file, per_input))

            stable_out = os.path.join(main.ARRANGE_DIR, f"{job_id}_packed.3mf")
            shutil.copy2(out_file, stable_out)
            background_tasks.add_task(main.cleanup_directory, job_dir)
            background_tasks.add_task(main.cleanup_file, stable_out)
            return FileResponse(
                path=stable_out,
                filename="packed.3mf",
                media_type="application/octet-stream",
            )

        except HTTPException:
            raise
        except Exception as e:
            background_tasks.add_task(main.cleanup_directory, job_dir)
            main.logger.exception("System error during pack operation")
            raise HTTPException(status_code=500, detail=f"System error: {e}")


def _mixed_selection(files: List[UploadFile], selection: str, solo_ok: bool
                     ) -> tuple[list[dict], list[str], dict[int, list[tuple[int, dict]]]]:
    """Validate a ``/pack/next``-shaped selection over .3mf/.stl uploads.

    Returns (entries, upload names, {upload index: [(entry index, selector without file)]}).
    """
    try:
        sel = json.loads(selection)
        if not isinstance(sel, list) or not sel or not all(isinstance(x, dict) and "file" in x for x in sel):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail='selection must be a non-empty JSON list of objects, each with a "file".')
    names: list[str] = []
    for f in files:
        try:
            n = main._safe_filename(f.filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not n.lower().endswith((".3mf", ".stl")):
            raise HTTPException(status_code=400, detail="Only .3mf and .stl files are supported.")
        names.append(n)
    per_file: dict[int, list[tuple[int, dict]]] = {}
    for g, item in enumerate(sel):
        rule = item.get("rule", "split")
        if rule not in _pack_next.RULES:
            raise HTTPException(status_code=422, detail=f"selection[{g}]: rule must be one of {list(_pack_next.RULES)}.")
        if rule == "solo" and not solo_ok:
            raise HTTPException(status_code=422, detail=(
                f"selection[{g}]: rule 'solo' is not packed with other parts; slice it as its own job."))
        qty = item.get("qty", 1)
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= main.PACK_MAX_INSTANCES:
            raise HTTPException(status_code=422, detail=(
                f"selection[{g}]: qty must be an integer from 1 to {main.PACK_MAX_INSTANCES}."))
        ref = item["file"]
        if isinstance(ref, int) and not isinstance(ref, bool):
            if not 0 <= ref < len(files):
                raise HTTPException(status_code=422, detail=f"selection[{g}]: file index {ref} out of range.")
            idx = ref
        else:
            hits = [i for i, n in enumerate(names) if n == os.path.basename(str(ref))]
            if len(hits) != 1:
                raise HTTPException(status_code=422, detail=(
                    f"selection[{g}]: file {ref!r} matches {len(hits)} uploads; use the 0-based index."))
            idx = hits[0]
        if names[idx].lower().endswith(".stl") and any(k in item for k in ("id", "name", "plate")):
            raise HTTPException(status_code=422, detail=f"selection[{g}]: an STL entry takes only file and qty.")
        per_file.setdefault(idx, []).append((g, {k: v for k, v in item.items() if k != "file"}))
    return sel, names, per_file


async def _mixed_project(job_dir: str, files: List[UploadFile], names: list[str],
                         per_file: dict[int, list[tuple[int, dict]]], bed_cfg: dict,
                         target: "main._Retarget") -> tuple[str, _pack_next.Origins, dict]:
    """Save the uploads into *job_dir* and merge the selection into one project on
    *bed_cfg*'s bed (``printable_area`` / ``printable_height``).

    Returns (merged path, {object id: [origin per instance]}, merge_subsets summary).
    """
    # 3MFs first so a project (with its filament slots) is the merge base, then STLs
    order = sorted(per_file, key=lambda i: (names[i].lower().endswith(".stl"), i))
    p = lambda n: os.path.join(job_dir, n)  # noqa: E731
    for i in order:
        with open(p(f"in_{i}{os.path.splitext(names[i])[1].lower()}"), "wb") as buf:
            await asyncio.to_thread(shutil.copyfileobj, files[i].file, buf)

    def _build():
        subs, file_origins, used = [], [], 0
        for i in order:
            stl = names[i].lower().endswith(".stl")
            src = p(f"in_{i}.stl" if stl else f"in_{i}.3mf")
            if stl:
                proj = p(f"stl_{i}.3mf")
                _pack_next.stl_project(src, proj, names[i], {
                    k: bed_cfg[k] for k in ("printable_area", "printable_height") if k in bed_cfg})
                src = proj
            # cap checked inside, before any per-instance allocation (incl. bundle x qty)
            want, origins = _pack_next.entry_origins(src, per_file[i], names[i], stl,
                                                     main.PACK_MAX_INSTANCES - used)
            used += sum(want.values())
            dst = p(f"sub_{i}.3mf")
            _subset.build_subset(src, dst, want, **target.kwargs)
            subs.append(dst)
            file_origins.append(origins)
        summary = _subset.merge_subsets(subs, p("merged.3mf"), labels=[names[i] for i in order])
        _pack_next.set_bed(p("merged.3mf"), bed_cfg)
        merged: _pack_next.Origins = {}
        for origins, id_map in zip(file_origins, summary["id_maps"]):
            for oid, lst in origins.items():
                merged[id_map[str(oid)]] = lst
        return merged, summary

    try:
        origins, summary = await asyncio.to_thread(_build)
    except _subset.SubsetError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot process input: {e}")
    return p("merged.3mf"), origins, summary


@main.app.post(
    "/api/v1/pack/next",
    tags=["arrange"],
    summary="The fullest single plate a machine can print from the remaining parts",
    description=(
        "Upload the remaining parts' sources as repeated `files` (`.3mf` and/or `.stl`) and a "
        "`selection` JSON list shaped like `/api/v1/projects/merge`: each entry names its `file` "
        "(uploaded filename or 0-based upload index) plus, for a 3MF, one of `id` / `name` / "
        "`plate`, and a `qty` (default 1); an STL entry is just `{file, qty}`. Optional `rule` "
        "per entry: `split` (default: instances may land anywhere), `together` (each copy of the "
        "entry, e.g. a `plate` bundle, is all-or-nothing on the returned plate; Orca chooses its "
        "layout) or `solo` (rejected with 422: send those as their own jobs).\n\n"
        "The selection is arranged by OrcaSlicer for `machine_uuid`'s bed and plate 1 is taken "
        "(no scoring of our own). A `together` copy Orca split across plates is dropped from the "
        "plate; if plate 1 holds more than `max_parts` instances, whole units are dropped, last "
        "placed first. Whenever anything is dropped, the kept instances are arranged again.\n\n"
        "**200:** a single-plate `.3mf`, footprint centred on the bed, ready for "
        "`POST /api/v1/jobs` with `plate=1`. It embeds `Metadata/laminus_manifest.json` (schema "
        "as for `/pack`, feature `pack.manifest`) whose `items` also carry `consumed`. Header "
        "`X-Consumed`: JSON array of instances taken per selection entry, by entry index.\n\n"
        "**422 `nothing_fits`** (`error.code`): nothing in the selection fits this bed; "
        "`error.details` lists the entries whose parts are too big. Every other failure uses "
        "another status or code. Blocking; each arrange is limited by `ARRANGE_TIMEOUT_SECONDS`."
    ),
    responses={
        400: {"description": "Unreadable 3MF/STL or Orca arrange failed"},
        408: {"description": "Arrange timed out"},
        422: {"description": "`nothing_fits`, or bad selection / rule / max_parts / machine_uuid"},
        503: {"description": "Catalog not ready"},
    },
    operation_id="v1_pack_next")
async def pack_next(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    selection: str = Form(..., description='JSON: [{"file": "a.3mf", "plate": 2, "qty": 1, "rule": "together"}, {"file": "b.stl", "qty": 4}]'),
    machine_uuid: str = Form(...),
    max_parts: Optional[int] = Form(None, description="Most instances on the returned plate"),
):
    if max_parts is not None and max_parts < 1:
        raise HTTPException(status_code=422, detail="max_parts must be >= 1.")
    sel, names, per_file = main._mixed_selection(files, selection, solo_ok=False)

    target = await main._resolve_retarget(machine_uuid, None, None)
    bed_rect = _plate_extract.bed_rect(target.printer_cfg)
    if bed_rect is None:
        raise HTTPException(status_code=422, detail=f"Machine UUID '{machine_uuid}' has no usable printable_area.")
    bed = (bed_rect[2] - bed_rect[0], bed_rect[3] - bed_rect[1],
           _plate_extract._printable_height(target.printer_cfg))

    job_id = str(uuid.uuid4())
    with main._track_inflight_arrange(job_id):
        job_dir = os.path.join(main.ARRANGE_DIR, f"next_{job_id}")
        os.makedirs(job_dir, exist_ok=True)
        background_tasks.add_task(main.cleanup_directory, job_dir)
        p = lambda n: os.path.join(job_dir, n)  # noqa: E731
        cur, origins, _ = await main._mixed_project(job_dir, files, names, per_file, target.printer_cfg, target)
        requested = _pack_next.consumed(origins, len(sel))

        def _nothing_fits(msg: str, details: list[dict]):
            return _api_v1.DetailedHTTPException(422, msg, "nothing_fits", details)

        # parts that can never fit this bed (and the together copies they belong to) are left out
        big = await asyncio.to_thread(_pack_next.oversize, cur, bed)
        if big:
            dropped_units = {o["unit"] for oid in big for o in origins.get(oid, [])}
            keep = {x for x in _pack_next.all_instances(origins)
                    if origins[x[0]][x[1]]["unit"] not in dropped_units}
            details = sorted({(o["item"], o["source"], o["part"], tuple(big[oid]))
                              for oid in big for o in origins.get(oid, [])})
            if not keep:
                bed_txt = " × ".join(f"{v:g}" for v in bed if v is not None)
                raise _nothing_fits(
                    f"Nothing in the selection fits the {bed_txt} mm bed of this machine",
                    [{"loc": ["body", "selection", it], "type": "nothing_fits",
                      "msg": f"{src} part {part} is {' × '.join(f'{v:g}' for v in dims)} mm"}
                     for it, src, part, dims in details])
            origins = await asyncio.to_thread(_pack_next.subset_instances, cur, p("fit.3mf"), origins, keep)
            cur = p("fit.3mf")

        final = None
        for rnd in range(_pack_next.MAX_ROUNDS):
            oracle, arranged = p(f"oracle_{rnd}.3mf"), p(f"arranged_{rnd}.3mf")
            await asyncio.to_thread(_subset.expand_instances, cur, oracle)
            await main._orca_arrange(oracle, arranged, False)
            try:
                _, plates = await asyncio.to_thread(_subset._arranged_layout, arranged)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Could not read the arranged layout: {e}")
            keep = _pack_next.choose(plates[0] if plates else [], origins, max_parts)
            if not keep:
                raise _nothing_fits("Nothing in the selection fits one plate of this machine "
                                    "(together bundles larger than the bed, or max_parts too small)", [])
            if len(plates) == 1 and len(keep) == len(_pack_next.all_instances(origins)):
                final = p(f"layout_{rnd}.3mf")
                try:
                    await asyncio.to_thread(_subset.transplant_layout, cur, arranged, final)
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Could not apply arranged layout: {e}")
                break
            nxt = p(f"keep_{rnd}.3mf")
            origins = await asyncio.to_thread(_pack_next.subset_instances, cur, nxt, origins, set(keep))
            cur = nxt
        if final is None:
            raise HTTPException(status_code=500, detail=(
                f"Arrange did not settle on one plate after {_pack_next.MAX_ROUNDS} rounds."))

        out = p("next.3mf")

        def _finish():
            with _subset.open_zip(final) as z:
                cfg = json.loads(z.read(_subset.PROJECT_SETTINGS))
            _plate_extract.retarget_plate(final, out, 1, None, cfg, centre_footprint=True)
            taken = _pack_next.consumed(origins, len(sel))
            m = _manifest.build_manifest(
                out, _manifest.keyed_origin(origins),
                [{"item": i, "source": names[per_item_file[i]], "requested": n}
                 for i, n in enumerate(taken) if n])
            m["items"] = [{"item": i, "source": names[per_item_file[i]], "requested": requested[i],
                           "consumed": taken[i]} for i in range(len(sel))]
            _manifest.embed_manifest(out, m)
            return taken

        per_item_file = {g: i for i, entries in per_file.items() for g, _ in entries}
        try:
            taken = await asyncio.to_thread(_finish)
        except Exception as e:
            main.logger.exception("pack/next finishing failed")
            raise HTTPException(status_code=500, detail=f"Could not finish the plate: {e}")
        stable_out = os.path.join(main.ARRANGE_DIR, f"{job_id}_next.3mf")
        shutil.copy2(out, stable_out)
        background_tasks.add_task(main.cleanup_file, stable_out)
        headers = {"X-Consumed": json.dumps(taken, separators=(",", ":")),
                   "X-Instance-Count": str(sum(taken))}
        return FileResponse(path=stable_out, filename="next_plate.3mf",
                            media_type="application/octet-stream", headers=headers)


@main.app.post(
    "/api/v1/pack/mixed",
    tags=["arrange"],
    summary="Arrange a mixed STL / 3MF selection on the fewest plates of one bed",
    description=(
        "Same `files` and `selection` as `/api/v1/pack/next` (`.3mf` entries `{file, id|name|plate, "
        "qty, rule}`, `.stl` entries `{file, qty, rule}`), for a bed given either as `bed_x` + "
        "`bed_y` + `bed_z` (mm; the projects' printer settings are kept, only the bed is replaced) "
        "or as `machine_uuid` (bed and printer retargeted as in `/api/v1/projects/subset`), not "
        "both.\n\n"
        "Rules: `split` (default) instances go wherever OrcaSlicer's arrange puts them; "
        "`together`: each copy of the entry (one `plate` bundle) lands on a single plate, Orca "
        "choosing its layout (a copy Orca split is taken off and arranged again with the other "
        "split copies); `solo`: every instance gets a plate of its own, footprint centred, after "
        "the arranged plates.\n\n"
        "**200:** one multi-plate `.3mf`; each plate slices with `POST /api/v1/jobs` `plate=N`. "
        "Per-object overrides, modifiers and paint of 3MF objects are kept (as for "
        "`/projects/merge`, whose base-project rules apply). Embeds `Metadata/laminus_manifest.json` "
        "(feature `pack.manifest`): `item` = selection index, `part` = source object id (3MF) or "
        "filename (STL), `requested` = qty x parts. Headers `X-Plate-Count`, `X-Instance-Count`, "
        "`X-Merge-Warnings`.\n\n"
        "**422 `part_too_large`** (`error.code`): a part larger than the bed, or a `together` copy "
        "that does not fit one plate; `error.details[].loc` is `[\"body\", \"selection\", <entry>]` "
        "per offending entry. Other input errors are 422 `unprocessable`. Blocking; each arrange "
        "is limited by `ARRANGE_TIMEOUT_SECONDS`."
    ),
    responses={
        400: {"description": "Unreadable 3MF/STL or Orca arrange failed"},
        408: {"description": "Arrange timed out"},
        422: {"description": "`part_too_large`, or bad selection / rule / bed / machine_uuid"},
        503: {"description": "machine_uuid given but catalog not ready"},
    },
    operation_id="v1_pack_mixed")
async def pack_mixed(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    selection: str = Form(..., description='JSON: [{"file": "a.3mf", "plate": 2, "qty": 2, "rule": "together"}, {"file": "b.stl", "qty": 4, "rule": "solo"}]'),
    machine_uuid: Optional[str] = Form(None),
    bed_x: Optional[float] = Form(None, description="Bed X in mm (with bed_y, bed_z; instead of machine_uuid)"),
    bed_y: Optional[float] = Form(None, description="Bed Y in mm"),
    bed_z: Optional[float] = Form(None, description="Printable height in mm"),
):
    sel, names, per_file = main._mixed_selection(files, selection, solo_ok=True)
    dims = (bed_x, bed_y, bed_z)
    if machine_uuid and any(v is not None for v in dims):
        raise HTTPException(status_code=422, detail="Give machine_uuid or bed_x/bed_y/bed_z, not both.")
    if machine_uuid:
        target = await main._resolve_retarget(machine_uuid, None, None)
        bed_cfg = target.printer_cfg
    elif all(v is not None for v in dims):
        if not all(v > 0 for v in dims):
            raise HTTPException(status_code=422, detail="bed_x, bed_y and bed_z must all be positive.")
        target = main._Retarget()
        bed_cfg = {"printable_area": ["0x0", f"{bed_x:g}x0", f"{bed_x:g}x{bed_y:g}", f"0x{bed_y:g}"],
                   "printable_height": bed_z}
    else:
        raise HTTPException(status_code=422, detail="Give machine_uuid or all of bed_x, bed_y, bed_z.")
    rect = _plate_extract.bed_rect(bed_cfg)
    if rect is None:
        raise HTTPException(status_code=422, detail=f"Machine UUID '{machine_uuid}' has no usable printable_area.")
    w, d = rect[2] - rect[0], rect[3] - rect[1]
    bed = (w, d, _plate_extract._printable_height(bed_cfg))
    bed_txt = " × ".join(f"{v:g}" for v in bed if v is not None)
    solo_items = {g for g, item in enumerate(sel) if item.get("rule") == "solo"}

    def _too_large(details: list[dict]):
        return _api_v1.DetailedHTTPException(
            422, f"Some of the selection does not fit one {bed_txt} mm plate", "part_too_large", details)

    job_id = str(uuid.uuid4())
    with main._track_inflight_arrange(job_id):
        job_dir = os.path.join(main.ARRANGE_DIR, f"mixed_{job_id}")
        os.makedirs(job_dir, exist_ok=True)
        background_tasks.add_task(main.cleanup_directory, job_dir)
        p = lambda n: os.path.join(job_dir, n)  # noqa: E731
        merged, origins, summary = await main._mixed_project(job_dir, files, names, per_file, bed_cfg, target)

        big = await asyncio.to_thread(_pack_next.oversize, merged, bed)
        if big:
            raise _too_large([
                {"loc": ["body", "selection", it], "type": "part_too_large",
                 "msg": f"{src} part {part} is {' × '.join(f'{v:g}' for v in size)} mm"}
                for it, src, part, size in sorted({(o["item"], o["source"], o["part"], tuple(big[oid]))
                                                   for oid in big for o in origins.get(oid, [])})])

        unit_of = lambda i: origins[i[0]][i[1]]["unit"]  # noqa: E731
        n_arranges = 0

        async def _arrange(pool: set) -> list[_pack_mixed.Placed]:
            """Orca's plates for just the *pool* instances of the merged project, plate-1 coordinates."""
            nonlocal n_arranges
            n_arranges += 1
            sub, oracle, arranged = (p(f"{s}_{n_arranges}.3mf") for s in ("pool", "oracle", "arranged"))
            back = _pack_mixed.renumber(pool)
            await asyncio.to_thread(_pack_next.subset_instances, merged, sub, origins, pool)
            await asyncio.to_thread(_subset.expand_instances, sub, oracle)
            await main._orca_arrange(oracle, arranged, False)
            try:
                tfs, plates = await asyncio.to_thread(_subset._arranged_layout, arranged)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Could not read the arranged layout: {e}")
            return _pack_mixed.to_local({back[i]: t for i, t in tfs.items()},
                                        [[back[i] for i in pl] for pl in plates], w, d)

        done: list[_pack_mixed.Placed] = []
        pool = {i for i in _pack_next.all_instances(origins) if origins[i[0]][i[1]]["item"] not in solo_items}
        for _ in range(_pack_next.MAX_ROUNDS):
            if not pool:
                break
            plates = await _arrange(pool)
            split = _pack_mixed.split_units(plates, origins)
            done += [kept for pl in plates if (kept := [x for x in pl if unit_of(x[0]) not in split])]
            left = {i for pl in plates for i, _ in pl if unit_of(i) in split}
            if left == pool:
                break                   # nothing settled: arrange those copies one by one below
            pool = left
        # still split: arrange one copy per entry on its own and reuse its layout for the
        # entry's other copies (identical), so the arrange count is bounded by entries
        copies: dict[int, list[set]] = {}
        for members in _pack_next.units(origins).values():
            if members & pool:
                copies.setdefault(origins[min(members)[0]][min(members)[1]]["item"], []).append(members)
        if len(copies) > _pack_mixed.MAX_COPY_ARRANGES:
            raise HTTPException(status_code=422, detail=(
                f"{len(copies)} together entries do not settle on single plates; "
                f"at most {_pack_mixed.MAX_COPY_ARRANGES} are re-arranged per request."))
        too_large: set[tuple[int, str]] = set()
        for item, units_ in sorted(copies.items()):
            plates = await _arrange(units_[0])
            if len(plates) != 1:
                too_large.add((item, origins[min(units_[0])[0]][min(units_[0])[1]]["source"]))
                continue
            done += [_pack_mixed.relabel(plates[0], units_[0], u) for u in units_]
        if too_large:
            raise _too_large([{"loc": ["body", "selection", it], "type": "part_too_large",
                               "msg": f"a together copy from {src} does not fit one plate"}
                              for it, src in sorted(too_large)])

        solo = sorted((i for i in _pack_next.all_instances(origins)
                       if origins[i[0]][i[1]]["item"] in solo_items),
                      key=lambda i: (origins[i[0]][i[1]]["item"], i))
        final = p("final.3mf")

        def _finish():
            plates = done + _pack_mixed.solo_plates(merged, solo, rect)
            tfs, lists = _pack_mixed.compose(plates, w, d)
            return _subset.apply_layout(merged, tfs, lists, final)

        try:
            n_plates = await asyncio.to_thread(_finish)
        except Exception as e:
            main.logger.exception("pack/mixed layout failed")
            raise HTTPException(status_code=500, detail=f"Could not apply the arranged layout: {e}")
        await main._embed_manifest(final, origins)
        stable_out = os.path.join(main.ARRANGE_DIR, f"{job_id}_mixed.3mf")
        shutil.copy2(final, stable_out)
        background_tasks.add_task(main.cleanup_file, stable_out)
        warnings = summary["warnings"] + await asyncio.to_thread(_preview.bed_clearance_warnings, final)
        headers = {"X-Plate-Count": str(n_plates),
                   "X-Instance-Count": str(len(_pack_next.all_instances(origins))),
                   "X-Merge-Warnings": " | ".join(warnings).encode("ascii", "replace").decode()}
        return FileResponse(path=stable_out, filename="mixed.3mf",
                            media_type="application/octet-stream", headers=headers)
