"""Profile routes: merged config, broken profiles, the neutral (model / nozzle-variant) lookups, upload and rescan.

Moved out of app/main.py. Everything that lives there (state such as `jobs` and `catalog`, config, shared helpers, and
the other route modules' functions, which main re-exports) is read as `main.<name>` at call time, so a test that patches
`app.main.<name>` still takes effect here. Routes register straight on `main.app`. See app/routes/README.md.
"""
import os
import shutil
import asyncio
from typing import Optional
from pydantic import BaseModel, Field
from fastapi import UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from app.project_config_builder import build_project_settings

import app.main as main


class MergedConfigRequest(BaseModel):
    machine_uuid: str = Field(
        ...,
        description=(
            "Stable UUID of the machine (printer) profile. Obtain from `GET /api/v1/profiles` "
            "or `GET /api/v1/profiles/{uuid}`. Machine UUIDs are derived from the "
            "(manufacturer, model, nozzle) tuple and are stable across catalog rebuilds."
        ),
    )
    process_uuid: str = Field(
        ...,
        description=(
            "Stable UUID of the process (print quality) profile, e.g. `0.20mm Standard`. "
            "Must be compatible with the supplied machine — check `compatible_printers` "
            "in the profile detail."
        ),
    )
    filament_uuids: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "List of filament profile UUIDs, one per extruder slot in slot order (slot 1 first). "
            "Must be compatible with the machine. Single-extruder printers need exactly one entry."
        ),
    )


@main.app.post(
    "/api/v1/profiles/merged-config",
    tags=["profiles"],
    summary="Get merged project_settings.config for a profile set",
    description=(
        "Resolves the full inheritance chains for the given machine, process, and filament "
        "UUIDs and returns the merged `project_settings.config` dict — exactly what would be "
        "embedded inside a `.3mf` before slicing.\n\n"
        "**Primary use cases:**\n"
        "- Inspect resolved print settings (temperatures, speeds, gcode) before committing to a "
        "slice job.\n"
        "- Allow Themis to diff embedded settings against a chosen preset to warn the user of "
        "overrides (`POST /api/v1/jobs/check-overrides`).\n"
        "- Verify that `layer_gcode` is populated (see OrcaSlicer -51 error) before slicing.\n\n"
        "The response is the raw merged dict with inheritance stripped — keys like `inherits`, "
        "`compatible_printers`, `is_custom_defined`, and `from` are removed. "
        "Multi-filament keys are returned as arrays (one value per slot).\n\n"
        "Returns **503** while the catalog is initialising."
    ),
    responses={
        422: {
            "description": (
                "UUID not found in catalog, wrong type (e.g. process UUID used where machine expected), "
                "or filament_uuids is empty"
            ),
        },
        503: {"description": "Catalog not yet ready — retry after /api/v1/health reports catalog_loaded: true"},
    },
    operation_id="v1_get_merged_config")
async def get_merged_config(body: MergedConfigRequest):
    if main.catalog is None:
        return JSONResponse(
            status_code=503,
            content={"status": "building_catalog", "detail": "Catalog not ready. Retry shortly."},
        )
    machine_entry = main.catalog.get_by_uuid(body.machine_uuid)
    if machine_entry is None or machine_entry.get("type") != "machine":
        raise HTTPException(status_code=422, detail=f"Machine UUID '{body.machine_uuid}' not found.")
    process_entry = main.catalog.get_by_uuid(body.process_uuid)
    if process_entry is None or process_entry.get("type") != "process":
        raise HTTPException(status_code=422, detail=f"Process UUID '{body.process_uuid}' not found.")
    if not body.filament_uuids:
        raise HTTPException(status_code=422, detail="filament_uuids must be a non-empty list.")
    filament_entries = []
    for fuid in body.filament_uuids:
        fe = main.catalog.get_by_uuid(fuid)
        if fe is None or fe.get("type") != "filament":
            raise HTTPException(status_code=422, detail=f"Filament UUID '{fuid}' not found.")
        filament_entries.append(fe)
    machine_resolved = main.catalog.resolved(machine_entry["uuid"])
    process_resolved = main.catalog.resolved(process_entry["uuid"])
    filament_resolved_list = [main.catalog.resolved(fe["uuid"]) for fe in filament_entries]
    config = await asyncio.to_thread(
        build_project_settings, machine_resolved, process_resolved, filament_resolved_list
    )
    return JSONResponse(content=config)


@main.app.get(
    "/api/v1/profiles/broken",
    tags=["profiles"],
    summary="List profiles excluded from the catalog",
    description=(
        "Profiles dropped by the last catalog build, each with the reason. An unresolvable "
        "`inherits` parent here is the signal that an OrcaSlicer update renamed or removed a "
        "vendor base that a user profile was building on.\n\n"
        "Must be registered before `/api/v1/profiles/{profile_uuid}` — Starlette matches path "
        "routes in registration order, and `{profile_uuid}` would otherwise swallow this path "
        "as a literal UUID lookup and always 404.\n\n"
        "Returns **503** while the catalog is still building."
    ),
    responses={503: {"description": "Catalog not yet ready"}},
    operation_id="v1_get_broken_profiles")
async def get_broken_profiles():
    if main.catalog is None or not main.catalog.is_built:
        return JSONResponse(
            status_code=503,
            content={"status": "building_catalog", "detail": "Catalog not ready. Retry shortly."},
        )
    broken = main.catalog.broken
    return {"count": len(broken), "broken": broken}


class NeutralModelQuery(BaseModel):
    manufacturer: str = Field(..., description="Printer manufacturer display name, e.g. 'Bambu Lab'.")
    model: str = Field(..., description="Printer model display name, e.g. 'P1S'.")
    model_key: Optional[str] = Field(None, description="Caller's own model key; matched alongside `model`.")


_NEUTRAL_RESPONSES = {503: {"description": "No catalog, live or cached: `{\"error\": \"catalog_unavailable\"}`"}}


@main.app.post(
    "/api/v1/profiles/machines/suggest",
    tags=["profiles"],
    summary="Suggest machine presets for a printer model",
    description=(
        "Candidate machine presets for (manufacturer, model), best first. Empty `candidates` "
        "(not 404) when nothing matches. `match_reason`: `exact_model`, `fuzzy_name`, "
        "`vendor_only` (same vendor, different model; low confidence, capped).\n\n"
        "Every response carries `catalog_version` (also as `X-Catalog-Version`); `stale: true` "
        "when served from a cached catalog while a fresh one is pending."
    ),
    responses=_NEUTRAL_RESPONSES,
    operation_id="v1_suggest_machines")
async def suggest_machines(body: NeutralModelQuery):
    cat, stale = main._neutral_catalog()
    if cat is None:
        return main._catalog_unavailable()
    candidates = cat.neutral.suggest_machines(body.manufacturer, body.model, body.model_key)
    return main._neutral_json(cat, stale, {"candidates": candidates})


@main.app.post(
    "/api/v1/profiles/models/variants",
    tags=["profiles"],
    summary="List nozzle variants of a printer model",
    description=(
        "Machine presets that are nozzle variants of the best-matching model, ordered by "
        "diameter then flow. Exactly one has `is_default_guess` (0.4 standard if present, else "
        "the smallest standard variant). Empty `variants` when the model is unknown."
    ),
    responses=_NEUTRAL_RESPONSES,
    operation_id="v1_model_variants")
async def model_variants(body: NeutralModelQuery):
    cat, stale = main._neutral_catalog()
    if cat is None:
        return main._catalog_unavailable()
    variants = cat.neutral.model_variants(body.manufacturer, body.model, body.model_key)
    return main._neutral_json(cat, stale, {"variants": variants})


@main.app.get(
    "/api/v1/profiles/machines/{machine_ref}/presets",
    tags=["profiles"],
    summary="Process/filament presets compatible with a machine",
    description=(
        "Presets whose resolved `compatible_printers` is empty or names this machine. "
        "`kind=process|filament` narrows; default both. `compatible_models` is omitted for "
        "universal presets. 404 `{\"error\": \"unknown_machine_ref\"}`, 422 `{\"error\": \"invalid_kind\"}`."
    ),
    responses={**_NEUTRAL_RESPONSES, 404: {"description": "unknown_machine_ref"}, 422: {"description": "invalid_kind"}},
    operation_id="v1_machine_presets")
async def machine_presets(machine_ref: str, kind: Optional[str] = None):
    cat, stale = main._neutral_catalog()
    if cat is None:
        return main._catalog_unavailable()
    if kind not in (None, "process", "filament"):
        return main._neutral_json(cat, stale, {"error": "invalid_kind"}, 422)
    machine = cat.neutral.machine_by_ref.get(machine_ref)
    if machine is None:
        return main._neutral_json(cat, stale, {"error": "unknown_machine_ref"}, 404)
    kinds = (kind,) if kind else ("process", "filament")
    return main._neutral_json(cat, stale, {
        "machine_ref": machine_ref, "presets": cat.neutral.machine_presets(machine, kinds),
    })


@main.app.get(
    "/api/v1/profiles/machines/{machine_ref}/supports",
    tags=["profiles"],
    summary="Can this machine ref (and bed) be sliced for?",
    description=(
        "Always 200. `supported` is false with `reason` `unknown_machine_ref` or `bed_mismatch` "
        "(`bed_x`/`bed_y` in mm, 1 mm tolerance; a machine with no declared bed size is never "
        "ruled out). `reason` is null when supported."
    ),
    responses=_NEUTRAL_RESPONSES,
    operation_id="v1_machine_supports")
async def machine_supports(machine_ref: str, bed_x: Optional[float] = None, bed_y: Optional[float] = None):
    cat, stale = main._neutral_catalog()
    if cat is None:
        return main._catalog_unavailable()
    supported, reason = cat.neutral.supports(machine_ref, bed_x, bed_y)
    return main._neutral_json(cat, stale, {"machine_ref": machine_ref, "supported": supported, "reason": reason})


@main.app.post(
    "/api/v1/profiles/upload",
    tags=["profiles"],
    summary="Upload a user profile JSON",
    description=(
        "Upload a flat OrcaSlicer preset JSON into the user config volume at "
        "`/config/user/default/{type}/`. A catalog rebuild is triggered in the background "
        "automatically — wait ~5 s and call `GET /api/v1/profiles` to confirm the profile "
        "appears before using it in a slice job.\n\n"
        "**Requirements for the JSON file:**\n"
        "- Must have a `.json` extension\n"
        "- `type` must be exactly `machine`, `process`, or `filament`\n\n"
        "**Prefer a thin preset over a flattened one.** Declare `inherits` naming the vendor "
        "base plus only the keys you are actually changing. Inheritance is resolved against "
        "the profile tree as it exists at slice time, so an OrcaSlicer update that improves "
        "the vendor base rolls straight through into your profile. A fully-flattened preset "
        "(no `inherits`) is frozen at the version it was flattened from and silently misses "
        "every later vendor fix — use it only when you want a permanent fork.\n\n"
        "If the base is renamed or removed by a later OrcaSlicer release the profile is "
        "dropped from the catalog rather than served half-resolved; `GET /api/v1/profiles/broken` "
        "reports it.\n\n"
        "**Profile persistence:** the uploaded file is written to a Docker volume "
        "(`/config/user/`). It survives container restarts but is not baked into the image. "
        "Re-upload after replacing the container with a clean image.\n\n"
        "**UUID stability:** user profiles receive a UUID derived from `(user, rel_path)` "
        "so the UUID changes if the file is renamed or moved."
    ),
    responses={
        400: {
            "description": (
                "Invalid `type` (must be machine, process, or filament), "
                "bad filename (null bytes, semicolons, path traversal), "
                "or file does not have a .json extension"
            ),
        },
    },
    operation_id="v1_upload_profile")
async def upload_profile(
    type: str = Form(...),
    file: UploadFile = File(...),
):
    if type not in ("machine", "process", "filament"):
        raise HTTPException(status_code=400, detail="type must be machine, process, or filament.")

    try:
        safe_name = main._safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not safe_name.endswith(".json"):
        raise HTTPException(status_code=400, detail="Profile file must be a .json file.")

    target_dir = os.path.join(main.USER_CONFIG_DIR, "default", type)
    os.makedirs(target_dir, exist_ok=True)
    target_file = os.path.join(target_dir, safe_name)
    with open(target_file, "wb") as buffer:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buffer)

    main._trigger_catalog_rebuild()

    return {
        "status": "success",
        "message": f"Profile uploaded to {type}/{safe_name}. Catalog rebuild started.",
        "filename": safe_name,
    }


@main.app.post(
    "/api/v1/profiles/rescan",
    tags=["profiles"],
    summary="Rescan profiles and rebuild the catalog",
    description=(
        "Re-walks the system and user profile trees, re-verifies every `inherits` chain, "
        "and rebuilds the catalog dictionary.\n\n"
        "The catalog is normally rebuilt automatically — the cache key covers the OrcaSlicer "
        "version, the vendor profile bundles, and every file under `/config/user/`, so any "
        "preset change voids it. Use this endpoint to force the work anyway: after editing "
        "profiles in place on a bind mount, or to re-check which profiles are broken after "
        "an OrcaSlicer upgrade.\n\n"
        "Returns immediately; the rebuild runs in the background. Poll `GET /api/v1/health` "
        "until `catalog_building` is `false`, then call `GET /api/v1/profiles/broken` for "
        "profiles whose inheritance no longer resolves."
    ),
    operation_id="v1_rescan_profiles")
async def rescan_profiles():
    already = main._catalog_building
    main._trigger_catalog_rebuild(force=True)
    return {
        "status": "already_building" if already else "started",
        "message": (
            "A catalog rebuild was already in progress; not queuing another."
            if already else
            "Catalog rescan started. Poll /api/v1/health until catalog_building is false."
        ),
    }
