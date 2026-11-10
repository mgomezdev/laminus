"""Service routes: the dashboard page, health, temp-file cleanup and the test-only known-profile lookup.

Moved out of app/main.py. Everything that lives there (state such as `jobs` and `catalog`, config, shared helpers, and
the other route modules' functions, which main re-exports) is read as `main.<name>` at call time, so a test that patches
`app.main.<name>` still takes effect here. Routes register straight on `main.app`. See app/routes/README.md.
"""
import os
import shutil
import asyncio
import time
from typing import List, Optional
from fastapi import HTTPException
from fastapi.responses import HTMLResponse

import app.main as main


@main.app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def get_dashboard():
    template_path = os.path.join(main.TEMPLATES_DIR, "index.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Frontend template index.html not found.")
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


def _orca_version() -> Optional[str]:
    """Installed OrcaSlicer release (e.g. "2.4.2"): the version entrypoint.sh actually
    extracted, falling back to the ORCA_VERSION env var; None if neither is known."""
    try:
        with open(main.ORCA_VERSION_FILE, encoding="utf-8") as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("ORCA_VERSION") or None


@main.app.get(
    "/api/v1/health",
    tags=["health"],
    summary="Service health check",
    description=(
        "Returns service readiness information. **Always call this first** — profile and "
        "slice endpoints return **503** while `catalog_loading` is `true`.\n\n"
        "**Response fields:**\n"
        "- `status` — always `\"healthy\"` when the process is running\n"
        "- `orcaslicer_installed` — whether the OrcaSlicer binary exists at "
        "`/usr/local/bin/orcaslicer`\n"
        "- `orcaslicer_version` — version string from `orcaslicer --version`, or `null` "
        "if the binary is not installed or timed out\n"
        "- `orca_version` — installed OrcaSlicer release (e.g. `\"2.4.2\"`), read from the "
        "install's `.version` marker, falling back to the `ORCA_VERSION` env var; `null` if "
        "neither is available\n"
        "- `config_mounted` — whether `/config` exists (volume mount check)\n"
        "- `system_profiles_available` — whether the OrcaSlicer AppImage's profiles "
        "directory is present (required for catalog build)\n"
        "- `catalog_loaded` — `true` when the profile catalog is fully built and ready; "
        "`false` during initial startup or rebuild\n"
        "- `catalog_building` — `true` while a catalog rebuild is in progress\n"
        "- `catalog_profile_count` — `{machine: N, process: N, filament: N}` when loaded, "
        "otherwise `null`\n"
        "- `catalog_skipped_count` — number of profiles excluded from the catalog on the "
        "last build (unresolvable `inherits` parent, unparseable JSON, etc.) — call "
        "`GET /api/v1/profiles/broken` for which profiles and why; `null` if not yet built\n"
        "- `active_jobs` — number of jobs currently in `slicing` status"
    ),
    operation_id="v1_health_check")
async def health_check():
    active = sum(1 for j in list(main.jobs.values()) if j["status"] == "slicing")
    return {
        "status": "healthy",
        "orcaslicer_installed": os.path.exists("/usr/local/bin/orcaslicer"),
        "orcaslicer_version": main._orcaslicer_version,
        "orca_version": main._orca_version(),
        "config_mounted": os.path.exists(main.CONFIG_DIR),
        "system_profiles_available": os.path.isdir(main.SYSTEM_PROFILES_DIR),
        "catalog_loaded": main.catalog is not None and main.catalog.is_built,
        "catalog_building": main._catalog_building,
        "catalog_profile_count": main.catalog.counts if (main.catalog and main.catalog.is_built) else None,
        "catalog_skipped_count": main.catalog.skipped_count if (main.catalog and main.catalog.is_built) else None,
        "active_jobs": active,
    }


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _scan_stale_temp(min_age_seconds: int) -> List[str]:
    """Top-level entries under JOBS_DIR/ARRANGE_DIR that are safe to delete.

    JOBS_DIR entries are only stale if their job_id isn't tracked by any active job
    (i.e. left behind by a crash/restart). ARRANGE_DIR entries (arrange/pack/thumbnail
    scratch dirs and stable output copies) are always synchronous and self-cleaning, so
    anything still there past min_age_seconds is orphaned from an interrupted request.
    Both are additionally gated by min_age_seconds to avoid racing a request in flight.
    """
    now = time.time()
    active_ids = set(main.jobs.keys())
    stale: List[str] = []

    if os.path.isdir(main.JOBS_DIR):
        for name in os.listdir(main.JOBS_DIR):
            if name in active_ids:
                continue
            path = os.path.join(main.JOBS_DIR, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age >= min_age_seconds:
                stale.append(path)

    if os.path.isdir(main.ARRANGE_DIR):
        for name in os.listdir(main.ARRANGE_DIR):
            if any(job_id in name for job_id in main._inflight_arrange_ids):
                continue
            path = os.path.join(main.ARRANGE_DIR, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age >= min_age_seconds:
                stale.append(path)

    return stale


@main.app.post(
    "/api/v1/cleanup",
    tags=["maintenance"],
    summary="Remove stale temp files from failed/interrupted jobs",
    description=(
        "Scans `/tmp/jobs` and `/tmp/arrange` for leftover directories/files not accounted "
        "for by any currently active job, and removes the ones older than `min_age_seconds`. "
        "This catches clutter that the normal job-eviction sweep can't see — e.g. scratch "
        "dirs from an `/api/v1/arrange`, `/api/v1/pack`, or `/api/v1/slice/thumbnail` request that "
        "never finished because the container was killed mid-request, or `/tmp/jobs` entries "
        "left behind by a slice job that existed before a restart.\n\n"
        "**`dry_run=true`** reports what would be removed (path count + total bytes) without "
        "deleting anything — use this to preview before committing.\n\n"
        "**`min_age_seconds`** (default 300) guards against deleting a directory that's still "
        "being written to by an in-flight request; pass `0` to remove everything unowned "
        "regardless of age."
    ),
    responses={
        422: {"description": "min_age_seconds must be >= 0"},
    },
    operation_id="v1_cleanup_temp")
async def cleanup_temp(min_age_seconds: int = 300, dry_run: bool = False):
    if min_age_seconds < 0:
        raise HTTPException(status_code=422, detail="min_age_seconds must be >= 0.")

    paths = await asyncio.to_thread(main._scan_stale_temp, min_age_seconds)

    items = []
    total_bytes = 0
    for path in paths:
        is_dir = os.path.isdir(path)
        try:
            size = await asyncio.to_thread(main._dir_size if is_dir else os.path.getsize, path)
        except OSError:
            size = 0
        total_bytes += size
        items.append({"path": os.path.relpath(path, "/tmp"), "bytes": size, "is_dir": is_dir})
        if not dry_run:
            if is_dir:
                await asyncio.to_thread(shutil.rmtree, path, True)
            else:
                await asyncio.to_thread(main.cleanup_file, path)

    if not dry_run and items:
        main.logger.info("Cleanup removed %d stale temp item(s), freed %d bytes", len(items), total_bytes)

    return {
        "dry_run": dry_run,
        "count": len(items),
        "freed_bytes": total_bytes,
        "items": items,
    }


@main.app.get("/api/v1/test/known-profile", include_in_schema=False)
async def test_known_profile(machine_name: str | None = None):
    """Return a machine/process/filament UUID triple from the loaded catalog.

    Without `machine_name`: the first compatible triple, for tests that need
    valid profile UUIDs without caring which hardware they land on.

    With `machine_name`: the triple for that exact machine, for tests seeding
    a printer that expects a specific model's profiles (e.g. a named
    placeholder printer). 404 if no machine in the catalog matches.

    Returns 503 while the catalog is building.
    """
    if main.catalog is None or not main.catalog.is_built:
        raise HTTPException(status_code=503, detail="Catalog not ready.")
    data = main.catalog.as_dict()
    machines = data.get("machine", [])
    processes = data.get("process", [])
    filaments = data.get("filament", [])
    if not machines or not processes or not filaments:
        raise HTTPException(status_code=503, detail="Catalog is empty.")
    if machine_name is not None:
        machine = next((m for m in machines if m["name"] == machine_name), None)
        if machine is None:
            raise HTTPException(status_code=404, detail=f"Machine {machine_name!r} not in catalog.")
    else:
        machine = machines[0]

    def _compatible(entry: dict) -> bool:
        cp = entry.get("compatible_printers", [])
        return not cp or machine["name"] in cp

    process = next((p for p in processes if _compatible(p)), processes[0])
    filament = next((f for f in filaments if _compatible(f)), filaments[0])
    return {
        "machine_uuid": machine["uuid"],
        "machine_name": machine["name"],
        "process_uuid": process["uuid"],
        "process_name": process["name"],
        "filament_uuid": filament["uuid"],
        "filament_name": filament["name"],
    }
