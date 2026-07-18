import os
import shutil
import asyncio
import uuid
import glob
import time
import logging
import json
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from app.profile_catalog import ProfileCatalog
from app.project_config_builder import build_project_settings, embed_project_settings
from app.stl_to_3mf import stl_to_3mf as _stl_to_3mf, inject_stls_into_3mf as _inject_stls_into_3mf, strip_application_version as _strip_app_version

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("laminus")

CONFIG_DIR = "/config"
USER_CONFIG_DIR = os.environ.get("USER_CONFIG_DIR", os.path.join(CONFIG_DIR, "user"))
DATA_DIR = "/data"
JOBS_DIR = "/tmp/jobs"
ARRANGE_DIR = "/tmp/arrange"

JOB_TTL = int(os.environ.get("JOB_TTL_SECONDS", "3600"))
JOB_SWEEP_INTERVAL = int(os.environ.get("JOB_SWEEP_INTERVAL_SECONDS", "300"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "4"))
THUMBNAIL_TIMEOUT = 120

SYSTEM_PROFILES_DIR = os.environ.get("SYSTEM_PROFILES_DIR", "/opt/orcaslicer/resources/profiles")
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT_SECONDS", "600"))
ARRANGE_TIMEOUT = int(os.environ.get("ARRANGE_TIMEOUT_SECONDS", "120"))

catalog: Optional[ProfileCatalog] = None
_catalog_building: bool = False
_orcaslicer_version: Optional[str] = None
_catalog_task: Optional[asyncio.Task] = None
# Template cache: maps (machine_uuid, process_uuid, filament_uuids_key) → 3MF bytes
_template_cache: Dict[str, bytes] = {}


# Fix R6: lifespan context runs startup/shutdown logic and background eviction task
@asynccontextmanager
async def lifespan(app: FastAPI):
    global catalog, _catalog_building, _orcaslicer_version, _catalog_task
    for d in (CONFIG_DIR, DATA_DIR, JOBS_DIR, ARRANGE_DIR):
        os.makedirs(d, exist_ok=True)
    init_config_directories()
    _load_jobs_on_startup()
    try:
        proc = await asyncio.create_subprocess_exec(
            "orcaslicer", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (asyncio.TimeoutError, Exception):
            proc.kill()
            await proc.wait()
            raise
        lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
        _orcaslicer_version = lines[0] if lines else None
    except Exception:
        _orcaslicer_version = None
    _catalog_building = True
    _catalog_task = asyncio.create_task(_build_catalog())
    sweep_task = asyncio.create_task(_evict_stale_jobs())
    try:
        yield
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass


def _trigger_catalog_rebuild() -> None:
    global _catalog_task, _catalog_building
    if not _catalog_building:
        _catalog_building = True
        _catalog_task = asyncio.create_task(_build_catalog())


async def _build_catalog():
    global catalog, _catalog_building, _template_cache
    try:
        cache_key = await asyncio.to_thread(_catalog_cache_key)
        cached = await asyncio.to_thread(
            ProfileCatalog.load_from_cache, _CATALOG_CACHE_FILE, cache_key,
            SYSTEM_PROFILES_DIR, USER_CONFIG_DIR,
        )
        if cached is not None:
            catalog = cached
            logger.info("Profile catalog loaded from cache: %s", cached.counts)
            return
        cat = ProfileCatalog(system_dir=SYSTEM_PROFILES_DIR, user_dir=USER_CONFIG_DIR)
        await asyncio.to_thread(cat.build)
        catalog = cat
        _template_cache.clear()
        logger.info("Profile catalog ready: %s", cat.counts)
        await asyncio.to_thread(cat.save_to_cache, _CATALOG_CACHE_FILE, cache_key)
    except Exception:
        logger.exception("Catalog build failed")
    finally:
        _catalog_building = False


app = FastAPI(
    title="Laminus — OrcaSlicer Sidecar API",
    description=(
        "Headless 3D model slicing via OrcaSlicer CLI running inside a Docker container. "
        "Laminus is a sidecar service used by Themis; it is not intended to be called directly "
        "by end-users in normal operation.\n\n"
        "## Typical agent workflow\n\n"
        "1. **`GET /api/health`** — wait until `catalog_loaded: true` before making any other calls.\n"
        "2. **`GET /api/profiles`** — discover machine / process / filament UUIDs for your printer "
        "and material. Filter by `manufacturer`, `model`, and `nozzle` to narrow results.\n"
        "3. **`POST /api/slice/start`** — upload the model (`.3mf` or `.stl`) with the resolved "
        "UUIDs. The response contains a `job_id`.\n"
        "4. **`GET /api/slice/status/{job_id}`** — poll every 2–5 s until `status` is `completed` "
        "or `failed`.\n"
        "5. **`GET /api/slice/download/{job_id}`** — retrieve the GCode (or `.3mf` when "
        "`export_3mf` was requested). Downloading evicts the job immediately.\n\n"
        "Alternatively, use **`POST /api/slice/prepared`** when the 3MF already embeds print "
        "settings from a previous OrcaSlicer session — no profile resolution is needed.\n\n"
        "## Packing / arrangement\n\n"
        "Use **`POST /api/pack`** to distribute STL geometry across build plates before slicing. "
        "Three modes: supply a template `.3mf`, profile UUIDs (preferred — catalog-aware), or "
        "explicit bed dimensions. The packed `.3mf` is returned directly (synchronous, up to "
        "120 s).\n\n"
        "## Error conventions\n\n"
        "- **422** — caller-side validation failure (bad UUIDs, missing required fields, etc.).\n"
        "- **503** — Laminus is not yet ready; the catalog is still building. "
        "Retry after `GET /api/health` reports `catalog_loaded: true`.\n"
        "- **400 / 408 / 500** — OrcaSlicer-side failures; check server logs for details."
    ),
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "health",
            "description": (
                "Service readiness and version information. Always check `/api/health` first — "
                "profile and slice endpoints return **503** while `catalog_loading` is `true`."
            ),
        },
        {
            "name": "profiles",
            "description": (
                "Machine, process, and filament preset catalog derived from the OrcaSlicer "
                "AppImage's bundled profiles plus any user-uploaded profiles in `/config/user/`. "
                "Profiles are identified by stable UUIDs derived from their (manufacturer, model, "
                "nozzle) tuple (machine) or (source, rel_path) (process/filament), so UUIDs "
                "remain constant across catalog rebuilds as long as the profile files don't move."
            ),
        },
        {
            "name": "slice",
            "description": (
                "Asynchronous slice job lifecycle: **start → poll → download**. "
                "Jobs are kept in memory and evicted after download or after `JOB_TTL_SECONDS` "
                "(default 3600). A maximum of `MAX_CONCURRENT_JOBS` (default 4) jobs may slice "
                "simultaneously; additional requests receive **503** until a slot opens."
            ),
        },
        {
            "name": "arrange",
            "description": (
                "Synchronous plate arrangement and STL packing. No job tracking — responses "
                "block until OrcaSlicer finishes (up to `ARRANGE_TIMEOUT_SECONDS`, default 120). "
                "Use `POST /api/pack` to combine multiple STLs into a single multi-plate `.3mf`, "
                "and `POST /api/arrange` to re-pack an existing `.3mf`."
            ),
        },
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _safe_filename(filename: Optional[str]) -> str:
    """Fix R1: guard against None; strip path components; reject control chars and semicolons."""
    # Fix R1: file.filename is Optional[str] in FastAPI — None when Content-Disposition omits filename
    if filename is None:
        raise ValueError("Uploaded file must include a filename.")
    name = os.path.basename(filename)
    if not name or name in (".", ".."):
        raise ValueError("Invalid filename.")
    # Fix R3: reject null bytes and newlines — both cause open() errors or log injection
    for bad in ("\x00", "\n", "\r"):
        if bad in name:
            raise ValueError("Filename contains disallowed control characters.")
    if ";" in name:
        raise ValueError(f"Filename must not contain semicolons: '{name}'")
    return name


class MergedConfigRequest(BaseModel):
    machine_uuid: str = Field(
        ...,
        description=(
            "Stable UUID of the machine (printer) profile. Obtain from `GET /api/profiles` "
            "or `GET /api/profiles/{uuid}`. Machine UUIDs are derived from the "
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


def init_config_directories():
    default_dirs = [
        os.path.join(USER_CONFIG_DIR, "default", "machine"),
        os.path.join(USER_CONFIG_DIR, "default", "process"),
        os.path.join(USER_CONFIG_DIR, "default", "filament"),
    ]
    for d in default_dirs:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            continue  # read-only bind mount — directories already exist
        readme_path = os.path.join(d, "README.txt")
        if not os.path.exists(readme_path):
            folder_type = os.path.basename(d)
            try:
                with open(readme_path, "w") as f:
                    f.write(f"Drop your OrcaSlicer {folder_type} JSON profiles in this directory.\n")
                    f.write("They will automatically appear in the Web UI / API list of profiles.\n")
            except OSError:
                pass  # read-only bind mount


jobs: Dict[str, dict] = {}

_JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
_CATALOG_CACHE_FILE = os.path.join(DATA_DIR, "catalog_cache.json")


def _catalog_cache_key() -> str:
    """Hash of ORCA_VERSION + sorted (relpath, mtime, size) for user config dir."""
    import hashlib
    h = hashlib.md5(os.environ.get("ORCA_VERSION", "").encode())
    if os.path.isdir(USER_CONFIG_DIR):
        entries = []
        for root, _dirs, files in os.walk(USER_CONFIG_DIR):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    st = os.stat(p)
                    entries.append((os.path.relpath(p, USER_CONFIG_DIR), st.st_mtime, st.st_size))
                except OSError:
                    pass
        for entry in sorted(entries):
            h.update(str(entry).encode())
    return h.hexdigest()


def _save_jobs() -> None:
    """Persist serialisable job metadata to disk (best-effort)."""
    serialisable = {
        jid: {"id": jid, "status": j["status"], "error": j.get("error"), "created_at": j.get("_wall_created_at")}
        for jid, j in jobs.items()
    }
    try:
        tmp = _JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serialisable, f)
        os.replace(tmp, _JOBS_FILE)
    except OSError:
        pass


def _load_jobs_on_startup() -> None:
    """Load persisted jobs; fail any that were in-progress when Laminus last stopped."""
    try:
        with open(_JOBS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    for jid, j in saved.items():
        if j.get("status") in ("pending", "slicing"):
            j["status"] = "failed"
            j["error"] = "Laminus restarted while job was in progress"
        # Restore as a minimal stub — no logger, no disk files (they're gone)
        jobs[jid] = {
            "id": jid,
            "status": j["status"],
            "error": j.get("error"),
            "sliced_file": None,
            "logger": None,
            "created_at": 0,
            "_wall_created_at": j.get("created_at"),
            "_stub": True,  # ponytail: stub jobs are status-only, download will 404
        }


# Fix R6: periodic sweep that removes jobs older than JOB_TTL and cleans their disk dirs
async def _evict_stale_jobs():
    while True:
        await asyncio.sleep(JOB_SWEEP_INTERVAL)
        cutoff = time.monotonic() - JOB_TTL
        stale = [jid for jid, j in list(jobs.items()) if j.get("created_at", 0) < cutoff]
        for jid in stale:
            j = jobs.pop(jid, None)
            if j:
                job_dir = os.path.join(JOBS_DIR, jid)
                await asyncio.to_thread(cleanup_directory, job_dir)
                logger.info(f"Evicted stale job {jid}")


class JobLogger:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self._logs: List[str] = []
        self._new_entry = asyncio.Event()
        self._done = False

    def log(self, message: str):
        msg = message.strip()
        if not msg:
            return
        self._logs.append(msg)
        self._new_entry.set()
        if msg == "__COMPLETED__" or msg.startswith("__FAILED__"):
            self._done = True

    async def get_stream(self):
        cursor = 0
        while True:
            while cursor < len(self._logs):
                msg = self._logs[cursor]
                cursor += 1
                yield f"data: {msg}\n\n"
                if msg == "__COMPLETED__" or msg.startswith("__FAILED__"):
                    return
            if self._done:
                return
            self._new_entry.clear()
            await self._new_entry.wait()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def get_dashboard():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Frontend template index.html not found.")
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.get(
    "/api/profiles",
    tags=["profiles"],
    summary="List profiles in the catalog",
    description=(
        "Returns all machine, process, and filament presets known to this container.\n\n"
        "**Filtering:** supply all three of `manufacturer`, `model`, and `nozzle` together "
        "to receive only the matching machine preset plus all process and filament presets "
        "that declare that machine in their `compatible_printers` list. Supplying only one "
        "or two of the three parameters is a **422** error.\n\n"
        "**Refresh:** pass `refresh=true` to schedule a background catalog rebuild "
        "(e.g., after uploading new profiles via `POST /api/profiles/upload`). The response "
        "reflects the *current* catalog immediately; the new catalog is available after ~5 s. "
        "While rebuilding, `GET /api/health` will show `catalog_building: true`.\n\n"
        "**Response shape:**\n"
        "```json\n"
        "{\n"
        '  "machine":  [{ "uuid": "...", "name": "...", "manufacturer": "...", "model": "...", "nozzle": "..." }],\n'
        '  "process":  [{ "uuid": "...", "name": "...", "layer_height": 0.2, "compatible_printers": [...] }],\n'
        '  "filament": [{ "uuid": "...", "name": "...", "filament_type": "PLA", "filament_colour": "#FFFFFF", ... }]\n'
        "}\n"
        "```\n\n"
        "Returns **503** while the catalog is initialising on container start."
    ),
    responses={
        422: {"description": "Partial filter: supply all three of manufacturer/model/nozzle or none"},
        503: {"description": "Catalog not yet ready — retry after /api/health reports catalog_loaded: true"},
    },
)
async def get_profiles(
    manufacturer: Optional[str] = None,
    model: Optional[str] = None,
    nozzle: Optional[str] = None,
    refresh: bool = False,
):
    if refresh:
        _trigger_catalog_rebuild()
    if catalog is None:
        return JSONResponse(
            status_code=503,
            content={"status": "building_catalog", "detail": "Catalog not ready. Retry shortly."},
        )
    tuple_params = [p for p in (manufacturer, model, nozzle) if p is not None]
    if tuple_params and len(tuple_params) != 3:
        raise HTTPException(
            status_code=422,
            detail="Provide all three of manufacturer, model, and nozzle together, or none.",
        )
    return catalog.as_dict(manufacturer=manufacturer, model=model, nozzle=nozzle)


@app.post(
    "/api/profiles/merged-config",
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
        503: {"description": "Catalog not yet ready — retry after /api/health reports catalog_loaded: true"},
    },
)
async def get_merged_config(body: MergedConfigRequest):
    if catalog is None:
        return JSONResponse(
            status_code=503,
            content={"status": "building_catalog", "detail": "Catalog not ready. Retry shortly."},
        )
    machine_entry = catalog.get_by_uuid(body.machine_uuid)
    if machine_entry is None or machine_entry.get("type") != "machine":
        raise HTTPException(status_code=422, detail=f"Machine UUID '{body.machine_uuid}' not found.")
    process_entry = catalog.get_by_uuid(body.process_uuid)
    if process_entry is None or process_entry.get("type") != "process":
        raise HTTPException(status_code=422, detail=f"Process UUID '{body.process_uuid}' not found.")
    if not body.filament_uuids:
        raise HTTPException(status_code=422, detail="filament_uuids must be a non-empty list.")
    filament_entries = []
    for fuid in body.filament_uuids:
        fe = catalog.get_by_uuid(fuid)
        if fe is None or fe.get("type") != "filament":
            raise HTTPException(status_code=422, detail=f"Filament UUID '{fuid}' not found.")
        filament_entries.append(fe)
    machine_resolved = machine_entry.get("_resolved", machine_entry)
    process_resolved = process_entry.get("_resolved", process_entry)
    filament_resolved_list = [fe.get("_resolved", fe) for fe in filament_entries]
    config = await asyncio.to_thread(
        build_project_settings, machine_resolved, process_resolved, filament_resolved_list
    )
    return JSONResponse(content=config)


@app.get(
    "/api/profiles/{profile_uuid}",
    tags=["profiles"],
    summary="Get full detail for one profile",
    description=(
        "Returns all public fields for a single profile entry identified by its stable UUID. "
        "The `_resolved` (inheritance-flattened) dict is not included — use "
        "`POST /api/profiles/merged-config` for the fully merged config.\n\n"
        "**Fields by type:**\n\n"
        "*Machine:* `uuid`, `name`, `manufacturer`, `model`, `nozzle`, `nozzle_diameter`, "
        "`bed_size_x`, `bed_size_y`, `extruder_count`, `source`, `rel_path`\n\n"
        "*Process:* `uuid`, `name`, `layer_height`, `compatible_printers`, `source`, `rel_path`\n\n"
        "*Filament:* `uuid`, `name`, `display_name`, `filament_type`, `filament_colour`, "
        "`filament_vendor`, `filament_diameter`, `filament_density`, `nozzle_temperature`, "
        "`nozzle_temperature_range_low`, `nozzle_temperature_range_high`, `bed_temperature`, "
        "`bed_temperature_initial_layer`, `compatible_printers`, `source`, `rel_path`\n\n"
        "Returns **503** while the catalog is initialising."
    ),
    responses={
        404: {"description": "Profile UUID not found in catalog"},
        503: {"description": "Catalog not yet ready — retry after /api/health reports catalog_loaded: true"},
    },
)
async def get_profile_detail(profile_uuid: str):
    if catalog is None:
        return JSONResponse(
            status_code=503,
            content={"status": "building_catalog", "detail": "Catalog not ready. Retry shortly."},
        )
    entry = catalog.get_by_uuid(profile_uuid)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Profile UUID '{profile_uuid}' not found.")
    return catalog._public(entry)


async def _stream_subprocess_output(process: asyncio.subprocess.Process, job_logger: JobLogger):
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


async def run_orcaslicer_task(
    job_id: str,
    input_file_path: str,
    output_dir: str,
    plate_id: int = 1,
    export_3mf: Optional[str] = None,
    geometry_only_retry: bool = True,
):
    job = jobs.get(job_id)
    if job is None:
        return
    job_logger = job["logger"]
    job["status"] = "slicing"
    _save_jobs()
    job_logger.log(f"Starting slice: {os.path.basename(input_file_path)}")

    async def _attempt(slice_input: str, label: str) -> bool:
        cmd = [
            "xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
            "orcaslicer", "--slice", str(plate_id),
            "--outputdir", output_dir, "--arrange", "1",
        ]
        if export_3mf:
            cmd.extend(["--export-3mf", export_3mf])
        cmd.append(slice_input)
        job_logger.log(f"{label}: {' '.join(cmd)}")
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                await asyncio.wait_for(_stream_subprocess_output(process, job_logger), timeout=SLICE_TIMEOUT)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                job_logger.log(f"ERROR: Timed out after {SLICE_TIMEOUT}s")
                return False
            else:
                await process.wait()
                return process.returncode == 0
        except Exception as exc:
            job_logger.log(f"SYSTEM ERROR: {exc}")
            logger.exception("Subprocess error in job %s", job_id)
            return False

    # OrcaSlicer 2.3.2 on Linux segfaults when the 3MF's Application metadata
    # contains a version string (e.g. "BambuStudio-2.3.2"). Strip it first.
    if input_file_path.lower().endswith(".3mf"):
        fixed_path = input_file_path[:-4] + "_fixed.3mf"
        try:
            await asyncio.to_thread(_strip_app_version, input_file_path, fixed_path)
            input_file_path = fixed_path
        except Exception as exc:
            job_logger.log(f"WARNING: could not strip Application version: {exc}")

    success = await _attempt(input_file_path, "Attempt 1")

    if not success and geometry_only_retry:
        job_logger.log("Attempt 1 failed - retrying with model_settings stripped")
        base, ext = os.path.splitext(input_file_path)
        geo_path = base + "_geo" + ext
        try:
            await _strip_model_settings(input_file_path, geo_path)
            success = await _attempt(geo_path, "Attempt 2 (geometry-only)")
        except Exception as exc:
            job_logger.log(f"ERROR stripping model_settings: {exc}")

    if success:
        if export_3mf:
            target = os.path.join(output_dir, export_3mf)
            found = target if os.path.exists(target) else None
        else:
            gcodes = sorted(glob.glob(os.path.join(output_dir, "*.gcode")))
            found = gcodes[0] if gcodes else None

        if found:
            job["status"] = "completed"
            job["sliced_file"] = found
            _save_jobs()
            job_logger.log(f"Output: {os.path.basename(found)}")
            job_logger.log("__COMPLETED__")
        else:
            job["status"] = "failed"
            job["error"] = "OrcaSlicer succeeded but no output file found."
            _save_jobs()
            job_logger.log("ERROR: No output file found.")
            job_logger.log("__FAILED__: Missing output file")
    else:
        job["status"] = "failed"
        job["error"] = "OrcaSlicer slice process failed. See logs."
        _save_jobs()
        job_logger.log("__FAILED__: OrcaSlicer returned non-zero")


@app.post(
    "/api/slice/start",
    tags=["slice"],
    summary="Start a slice job (UUID-based profile resolution)",
    description=(
        "Upload a `.3mf` or `.stl` and specify print settings by profile UUID. "
        "The API resolves each profile's full inheritance chain, merges machine + process + "
        "filament into a `project_settings.config`, embeds it into the 3MF, then launches "
        "OrcaSlicer in the background and returns a `job_id` immediately.\n\n"
        "**Machine identification (mutually exclusive):**\n"
        "- `machine_uuid` — preferred; stable across catalog rebuilds.\n"
        "- `manufacturer` + `model` + `nozzle` — tuple lookup; useful when the UUID is unknown.\n\n"
        "**Key parameters:**\n"
        "- `filament_uuids` — JSON array string, e.g. `'[\"uuid1\"]'`; one entry per extruder "
        "slot in slot order. Single-extruder printers need exactly one entry.\n"
        "- `plate` — 1-based plate number to slice. Use `1` for single-plate models.\n"
        "- `export_3mf` — optional output filename. When set, OrcaSlicer embeds the GCode "
        "inside a `.3mf` instead of writing a standalone `.gcode` file. The download endpoint "
        "returns the `.3mf` in this case.\n"
        "- `geometry_only_retry` — when `true` (default), if the first slice attempt fails "
        "OrcaSlicer retries with `Metadata/model_settings.config` stripped from the 3MF. "
        "This resolves failures caused by stale extruder-slot assignments baked into models "
        "exported from OrcaSlicer.\n"
        "- `extra_config` — optional JSON object merged into the resolved "
        "`project_settings.config` after profile resolution. Use to override individual "
        "settings, e.g. `{\"curr_bed_type\": \"Cool Plate\"}` or "
        "`{\"layer_height\": \"0.12\"}`.\n\n"
        "**STL files** are automatically converted to a minimal 3MF before slicing.\n\n"
        "**Concurrency:** at most `MAX_CONCURRENT_JOBS` (default 4) jobs may be slicing "
        "simultaneously. When the limit is reached the request returns **503** — retry later.\n\n"
        "**Job lifecycle after creation:**\n"
        "```\n"
        "pending → slicing → completed  (download available)\n"
        "                  ↘ failed     (check error field in status response)\n"
        "```\n"
        "Poll `GET /api/slice/status/{job_id}` every 2–5 s, then download via "
        "`GET /api/slice/download/{job_id}` when complete."
    ),
    responses={
        400: {"description": "Bad filename (null bytes, semicolons, or missing Content-Disposition filename)"},
        422: {
            "description": (
                "Validation failure: plate < 1, filament_uuids is not a valid JSON array, "
                "machine/process/filament UUID not found in catalog, profile not compatible "
                "with the selected machine, or extra_config is not a JSON object"
            ),
        },
        503: {
            "description": (
                "Profile catalog not yet ready (retry after catalog_loaded: true), "
                "or too many concurrent slice jobs (retry when a slot opens)"
            ),
        },
    },
)
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
    geometry_only_retry: bool = Form(True),
    extra_config: Optional[str] = Form(None, description='JSON object merged into project settings after resolution'),
):
    if catalog is None:
        raise HTTPException(status_code=503, detail="Profile catalog not yet ready.")

    active_count = sum(1 for j in jobs.values() if j["status"] == "slicing")
    if active_count >= MAX_CONCURRENT_JOBS:
        raise HTTPException(status_code=503, detail=f"Too many active jobs ({active_count}/{MAX_CONCURRENT_JOBS}). Retry later.")

    try:
        fil_uuid_list: list[str] = json.loads(filament_uuids)
        if not isinstance(fil_uuid_list, list) or not fil_uuid_list:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="filament_uuids must be a non-empty JSON array.")

    if plate < 1:
        raise HTTPException(status_code=422, detail="plate must be >= 1.")

    # Machine lookup: prefer stable UUID, fall back to (manufacturer, model, nozzle) tuple.
    if machine_uuid:
        machine_entry = catalog.get_by_uuid(machine_uuid)
        if machine_entry is None or machine_entry.get("type") != "machine":
            raise HTTPException(status_code=422, detail=f"Machine UUID '{machine_uuid}' not found in catalog.")
    elif all(x is not None for x in (manufacturer, model, nozzle)):
        machine_entry = catalog.get_machine(manufacturer, model, nozzle)
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

    process_entry = catalog.get_by_uuid(process_uuid)
    if process_entry is None or process_entry.get("type") != "process":
        raise HTTPException(status_code=422, detail=f"Process UUID '{process_uuid}' not found.")

    filament_entries = []
    for fuid in fil_uuid_list:
        fe = catalog.get_by_uuid(fuid)
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
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    raw_path = os.path.join(input_dir, safe_name)
    with open(raw_path, "wb") as buf:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buf)

    if safe_name.lower().endswith(".stl"):
        base_3mf = os.path.join(input_dir, os.path.splitext(safe_name)[0] + ".3mf")
        await asyncio.to_thread(_stl_to_3mf, raw_path, base_3mf)
    else:
        base_3mf = raw_path

    machine_resolved = machine_entry.get("_resolved", machine_entry)
    process_resolved = process_entry.get("_resolved", process_entry)
    filament_resolved_list = [fe.get("_resolved", fe) for fe in filament_entries]

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
    prepared_3mf = os.path.join(input_dir, "prepared.3mf")
    await asyncio.to_thread(embed_project_settings, base_3mf, project_cfg, prepared_3mf)

    job_logger = JobLogger(job_id)
    _wall_now = time.time()
    jobs[job_id] = {
        "id": job_id, "status": "pending",
        "input_file": prepared_3mf, "output_dir": output_dir,
        "sliced_file": None, "output_format": "gcode_3mf" if export_3mf else "gcode",
        "error": None, "logger": job_logger, "created_at": time.monotonic(),
        "_wall_created_at": _wall_now,
    }
    _save_jobs()
    background_tasks.add_task(
        run_orcaslicer_task,
        job_id=job_id, input_file_path=prepared_3mf, output_dir=output_dir,
        plate_id=plate, export_3mf=export_3mf, geometry_only_retry=geometry_only_retry,
    )
    return {"job_id": job_id, "status": "pending", "message": "Slicing job started."}


@app.post(
    "/api/slice/prepared",
    tags=["slice"],
    summary="Slice a pre-configured 3MF (no profile resolution)",
    description=(
        "Accepts a `.3mf` that already contains `Metadata/project_settings.config` "
        "(e.g., exported from OrcaSlicer desktop or produced by `POST /api/slice/start` "
        "with `export_3mf` set). No catalog lookup or profile merging is performed — "
        "OrcaSlicer uses the embedded settings verbatim.\n\n"
        "Use this endpoint when you already have a fully-configured 3MF and want to avoid "
        "the overhead of profile resolution. Typical callers: Themis queue engine dispatching "
        "pre-packed project 3MFs, or debugging with a known-good 3MF from the desktop app.\n\n"
        "**Geometry-only retry** works the same as in `POST /api/slice/start`: when "
        "`geometry_only_retry=true` (default) and the first slice fails, the API retries "
        "with `Metadata/model_settings.config` stripped from the 3MF.\n\n"
        "**`export_3mf`:** when set, OrcaSlicer embeds the sliced GCode into a `.3mf` and "
        "the download endpoint returns that file instead of a standalone `.gcode`.\n\n"
        "The response and job lifecycle are identical to `POST /api/slice/start`."
    ),
    responses={
        400: {"description": "Bad filename (null bytes, semicolons, or missing Content-Disposition filename)"},
        422: {"description": "File is not a .3mf, or plate < 1"},
        503: {
            "description": (
                "Too many concurrent slice jobs — retry when a slot opens "
                "(check active_jobs in GET /api/health)"
            ),
        },
    },
)
async def slice_prepared(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    plate: int = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: bool = Form(True),
):
    if plate < 1:
        raise HTTPException(status_code=422, detail="plate must be >= 1.")

    active_count = sum(1 for j in jobs.values() if j["status"] == "slicing")
    if active_count >= MAX_CONCURRENT_JOBS:
        raise HTTPException(status_code=503, detail=f"Too many active jobs ({active_count}/{MAX_CONCURRENT_JOBS}). Retry later.")

    try:
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=422, detail="Only .3mf files accepted here.")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    input_path = os.path.join(input_dir, safe_name)
    with open(input_path, "wb") as buf:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buf)

    job_logger = JobLogger(job_id)
    jobs[job_id] = {
        "id": job_id, "status": "pending",
        "input_file": input_path, "output_dir": output_dir,
        "sliced_file": None, "output_format": "gcode_3mf" if export_3mf else "gcode",
        "error": None, "logger": job_logger, "created_at": time.monotonic(),
        "_wall_created_at": time.time(),
    }
    _save_jobs()
    background_tasks.add_task(
        run_orcaslicer_task,
        job_id=job_id, input_file_path=input_path, output_dir=output_dir,
        plate_id=plate, export_3mf=export_3mf, geometry_only_retry=geometry_only_retry,
    )
    return {"job_id": job_id, "status": "pending", "message": "Slice job started."}


@app.post(
    "/api/slice/thumbnail",
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
)
async def slice_thumbnail(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    plate: int = Form(...),
):
    if plate < 1:
        raise HTTPException(status_code=422, detail={"error": "plate must be >= 1."})
    try:
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": str(e)})
    if not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=422, detail={"error": "Only .3mf files accepted."})

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(ARRANGE_DIR, f"thumb_{job_id}")
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
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=THUMBNAIL_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            background_tasks.add_task(cleanup_directory, job_dir)
            raise HTTPException(status_code=422, detail={"error": f"Timed out after {THUMBNAIL_TIMEOUT}s"})

        if process.returncode != 0 or not os.path.exists(out_3mf):
            log_text = stdout.decode("utf-8", errors="replace") if stdout else ""
            logger.error("Thumbnail failed. Exit %s. Log: %s", process.returncode, log_text)
            background_tasks.add_task(cleanup_directory, job_dir)
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
        background_tasks.add_task(cleanup_directory, job_dir)

        if png_bytes is None:
            raise HTTPException(status_code=422, detail={"error": "No plate PNG found in OrcaSlicer output"})

        return Response(content=png_bytes, media_type="image/png")

    except HTTPException:
        raise
    except Exception as exc:
        background_tasks.add_task(cleanup_directory, job_dir)
        logger.exception("Thumbnail error")
        raise HTTPException(status_code=422, detail={"error": str(exc)})


@app.get(
    "/api/slice/status/{job_id}",
    tags=["slice"],
    summary="Poll slice job status",
    description=(
        "Returns the current status of a slice job. Poll every 2–5 seconds until `status` "
        "is `completed` or `failed`.\n\n"
        "**Response fields:**\n"
        "- `job_id` — echoed back for correlation\n"
        "- `status` — one of `pending`, `slicing`, `completed`, `failed`\n"
        "- `output_format` — `gcode` (default) or `gcode_3mf` (when `export_3mf` was set)\n"
        "- `sliced_file` — basename of the output file when `completed`, otherwise `null`\n"
        "- `error` — human-readable error message when `failed`, otherwise `null`\n\n"
        "**Job lifecycle:** `pending` → `slicing` → `completed` or `failed`\n\n"
        "Jobs are kept for `JOB_TTL_SECONDS` (default 3600) or until downloaded. "
        "A **404** on a previously-valid `job_id` means the job has already been evicted "
        "— either downloaded or timed out."
    ),
    responses={
        404: {"description": "Job not found — never existed, already downloaded, or evicted after TTL"},
    },
)
async def get_job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    job = jobs[job_id]
    return {
        "job_id": job["id"],
        "status": job["status"],
        "output_format": job.get("output_format", "gcode"),
        "sliced_file": os.path.basename(job["sliced_file"]) if job["sliced_file"] else None,
        "error": job["error"],
    }


@app.get(
    "/api/slice/logs/{job_id}",
    tags=["slice"],
    summary="Stream slice job logs (Server-Sent Events)",
    description=(
        "Returns a Server-Sent Events stream of OrcaSlicer stdout/stderr lines, "
        "emitted in real time as the slicer runs.\n\n"
        "**Event format:** `data: <log line>\\r\\n\\r\\n`\n\n"
        "**Terminal events (stream ends after these):**\n"
        "- `data: __COMPLETED__` — job finished successfully\n"
        "- `data: __FAILED__: <reason>` — job failed; `<reason>` is a short label\n\n"
        "**Usage notes:**\n"
        "- The stream replays all buffered log lines immediately on connect, so connecting "
        "after the job has already completed still delivers the full log history.\n"
        "- Most callers can skip this and poll `GET /api/slice/status` instead — logs are "
        "primarily useful for debugging slice failures.\n"
        "- If the job is evicted before you connect, the endpoint returns **404**."
    ),
    response_class=StreamingResponse,
    responses={
        404: {"description": "Job not found — never existed, already downloaded, or evicted after TTL"},
    },
)
async def get_job_logs(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    job = jobs[job_id]
    return StreamingResponse(job["logger"].get_stream(), media_type="text/event-stream")


@app.get(
    "/api/slice/download/{job_id}",
    tags=["slice"],
    summary="Download sliced output file",
    description=(
        "Returns the sliced output as `application/octet-stream`. The file extension "
        "indicates the format:\n"
        "- `.gcode` — standalone GCode file (default)\n"
        "- `.3mf` — GCode embedded inside a 3MF (when `export_3mf` was set during job creation)\n\n"
        "**⚠️ One-time download:** downloading evicts the job immediately — the in-memory "
        "record and all temporary files on disk are deleted. There is no second download. "
        "Only call this once you have confirmed `status == 'completed'`.\n\n"
        "Returns **400** if the job exists but is not yet complete (wrong status). "
        "Returns **404** if the job does not exist (never created, already downloaded, or evicted)."
    ),
    responses={
        400: {"description": "Job exists but is not yet complete — check status first"},
        404: {
            "description": (
                "Job not found (never existed, already downloaded, or evicted after TTL), "
                "or job is complete but the output file is missing from disk"
            ),
        },
    },
)
async def download_sliced_file(job_id: str, background_tasks: BackgroundTasks):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    job = jobs[job_id]
    if job["status"] != "completed" or not job["sliced_file"]:
        raise HTTPException(status_code=400, detail=f"Slicing job is not complete. Current status: {job['status']}")
    file_path = job["sliced_file"]
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Sliced file was not found on disk.")
    filename = os.path.basename(file_path)
    # Fix R6: evict job from dict and clean up disk after download
    background_tasks.add_task(_evict_job, job_id)
    return FileResponse(path=file_path, filename=filename, media_type="application/octet-stream")


def _evict_job(job_id: str):
    """Remove a job from the in-memory dict and clean its working directory."""
    j = jobs.pop(job_id, None)
    if j:
        cleanup_directory(os.path.join(JOBS_DIR, job_id))


def cleanup_directory(path: str):
    if os.path.exists(path):
        shutil.rmtree(path)
        logger.info(f"Cleaned up temp folder: {path}")


def cleanup_file(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


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


@app.post(
    "/api/pack",
    tags=["arrange"],
    summary="Pack N STLs onto as many plates as needed (synchronous)",
    description=(
        "Distributes STL geometry across build plates via OrcaSlicer `--arrange 1 --orient 1`. "
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
        "**3. Explicit bed dimensions** (`bed_x` + `bed_y` + `bed_z`, all in mm)\n"
        "Laminus constructs a minimal bed template from the supplied dimensions. "
        "`bed_z` is the printable height (not the physical Z travel). "
        "No profile catalog or template file needed — useful when you want geometry-only "
        "packing without print settings.\n\n"
        "## STL inputs\n\n"
        "- `files` — one or more `.stl` files (max 50 per request)\n"
        "- Filenames must not contain null bytes, newlines, or semicolons\n"
        "- Duplicate filenames in the same batch are deduplicated with a numeric suffix\n\n"
        "## Notes\n\n"
        "- OrcaSlicer auto-orients objects (`--orient 1`) to minimise support material. "
        "Pass orient=false to skip (not yet exposed as a parameter — file a request).\n"
        "- The packed `.3mf` preserves `Metadata/project_settings.config` from the template "
        "but discards any geometry or model_settings from it.\n"
        "- All temporary files are cleaned up after the response is sent."
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
                "UUID not found in catalog, or no settings source provided"
            ),
        },
        500: {"description": "Unexpected system error — check server logs"},
        503: {"description": "Profile catalog not yet ready (UUID mode only)"},
    },
)
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
):
    if not files:
        raise HTTPException(status_code=422, detail="At least one STL file is required.")
    if len(files) > 50:
        raise HTTPException(status_code=422, detail="Maximum 50 STL files per request.")

    # Validate STL filenames
    safe_names: list[str] = []
    for f in files:
        try:
            name = _safe_filename(f.filename)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not name.lower().endswith(".stl"):
            raise HTTPException(status_code=422, detail=f"Only .stl files accepted; got: {name}")
        safe_names.append(name)

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(ARRANGE_DIR, f"pack_{job_id}")
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # --- Resolve template ---
    if template is not None:
        # Caller-supplied template file
        try:
            template_name = _safe_filename(template.filename)
        except ValueError as e:
            background_tasks.add_task(cleanup_directory, job_dir)
            raise HTTPException(status_code=400, detail=str(e))
        if not template_name.lower().endswith(".3mf"):
            background_tasks.add_task(cleanup_directory, job_dir)
            raise HTTPException(status_code=422, detail="template must be a .3mf file.")
        template_path = os.path.join(input_dir, template_name)
        with open(template_path, "wb") as buf:
            await asyncio.to_thread(shutil.copyfileobj, template.file, buf)

    elif machine_uuid and process_uuid and filament_uuids:
        # UUID-based: resolve profiles from catalog and build template (cached)
        if catalog is None:
            background_tasks.add_task(cleanup_directory, job_dir)
            raise HTTPException(status_code=503, detail="Profile catalog not yet ready.")
        try:
            fil_uuid_list: list[str] = json.loads(filament_uuids)
            if not isinstance(fil_uuid_list, list) or not fil_uuid_list:
                raise ValueError
        except (ValueError, TypeError):
            background_tasks.add_task(cleanup_directory, job_dir)
            raise HTTPException(status_code=422, detail="filament_uuids must be a non-empty JSON array.")

        cache_key = f"{machine_uuid}|{process_uuid}|{','.join(sorted(fil_uuid_list))}"
        cached_bytes = _template_cache.get(cache_key)

        if cached_bytes is None:
            machine_entry = catalog.get_by_uuid(machine_uuid)
            if machine_entry is None or machine_entry.get("type") != "machine":
                background_tasks.add_task(cleanup_directory, job_dir)
                raise HTTPException(status_code=422, detail=f"Machine UUID '{machine_uuid}' not found.")
            proc_entry = catalog.get_by_uuid(process_uuid)
            if proc_entry is None or proc_entry.get("type") != "process":
                background_tasks.add_task(cleanup_directory, job_dir)
                raise HTTPException(status_code=422, detail=f"Process UUID '{process_uuid}' not found.")
            fil_entries = []
            for fuid in fil_uuid_list:
                fe = catalog.get_by_uuid(fuid)
                if fe is None or fe.get("type") != "filament":
                    background_tasks.add_task(cleanup_directory, job_dir)
                    raise HTTPException(status_code=422, detail=f"Filament UUID '{fuid}' not found.")
                fil_entries.append(fe)

            project_cfg = await asyncio.to_thread(
                build_project_settings,
                machine_entry.get("_resolved", machine_entry),
                proc_entry.get("_resolved", proc_entry),
                [fe.get("_resolved", fe) for fe in fil_entries],
            )

            cached_bytes = await asyncio.to_thread(
                _build_bed_template_bytes,
                project_cfg["printable_area"],
                project_cfg["printable_height"],
            )
            _template_cache[cache_key] = cached_bytes
            logger.info("Template cached for key %s (%d bytes)", cache_key[:40], len(cached_bytes))
        else:
            logger.debug("Template cache hit for key %s", cache_key[:40])

        template_path = os.path.join(input_dir, "settings_template.3mf")
        with open(template_path, "wb") as buf:
            buf.write(cached_bytes)

    elif bed_x is not None and bed_y is not None and bed_z is not None:
        if bed_x <= 0 or bed_y <= 0 or bed_z <= 0:
            background_tasks.add_task(cleanup_directory, job_dir)
            raise HTTPException(status_code=422, detail="bed_x, bed_y, and bed_z must all be positive.")
        area = [f"0x0", f"{bed_x}x0", f"{bed_x}x{bed_y}", f"0x{bed_y}"]
        template_path = os.path.join(input_dir, "settings_template.3mf")
        with open(template_path, "wb") as buf:
            buf.write(_build_bed_template_bytes(area, bed_z))

    else:
        background_tasks.add_task(cleanup_directory, job_dir)
        raise HTTPException(
            status_code=422,
            detail="Provide 'template' (a .3mf file) OR 'machine_uuid'/'process_uuid'/'filament_uuids' OR 'bed_x'/'bed_y'/'bed_z'.",
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
        await asyncio.to_thread(_inject_stls_into_3mf, template_path, stl_paths, combined_3mf)
    except ValueError as e:
        background_tasks.add_task(cleanup_directory, job_dir)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        background_tasks.add_task(cleanup_directory, job_dir)
        raise HTTPException(status_code=400, detail=f"Failed to build combined 3MF: {e}")

    out_file = os.path.join(output_dir, "packed.3mf")
    cmd = [
        "xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
        "orcaslicer",
        "--datadir", CONFIG_DIR,
        "--arrange", "1",
        "--orient", "1",
        "--export-3mf", out_file,
        combined_3mf,
    ]
    logger.info("Running pack command: %s", " ".join(cmd))

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=120.0)
        exit_code = process.returncode

        if exit_code != 0 or not os.path.exists(out_file):
            logger.error(
                "Pack failed. Exit %d. Log:\n%s",
                exit_code,
                stdout.decode("utf-8", errors="replace") if stdout else "(no output)",
            )
            background_tasks.add_task(cleanup_directory, job_dir)
            raise HTTPException(
                status_code=400,
                detail=f"Slicer pack failed (exit {exit_code}). Check server logs.",
            )

        stable_out = os.path.join(ARRANGE_DIR, f"{job_id}_packed.3mf")
        shutil.copy2(out_file, stable_out)
        background_tasks.add_task(cleanup_directory, job_dir)
        background_tasks.add_task(cleanup_file, stable_out)
        return FileResponse(
            path=stable_out,
            filename="packed.3mf",
            media_type="application/octet-stream",
        )

    except asyncio.TimeoutError:
        background_tasks.add_task(cleanup_directory, job_dir)
        raise HTTPException(status_code=408, detail="Pack operation timed out after 120 seconds.")
    except HTTPException:
        raise
    except Exception as e:
        background_tasks.add_task(cleanup_directory, job_dir)
        logger.exception("System error during pack operation")
        raise HTTPException(status_code=500, detail=f"System error: {e}")


@app.post(
    "/api/arrange",
    tags=["arrange"],
    summary="Auto-arrange and orient objects in a 3MF (synchronous)",
    description=(
        "Runs OrcaSlicer's plate-packing (`--arrange 1`) and auto-orientation "
        "(`--orient 1`) on an existing `.3mf` and returns the rearranged `.3mf` directly "
        "as `application/octet-stream`.\n\n"
        "**Blocking:** waits up to `ARRANGE_TIMEOUT_SECONDS` (default 120 s) for OrcaSlicer "
        "to finish. No job tracking — call completes synchronously.\n\n"
        "**When to use this vs `POST /api/pack`:**\n"
        "- Use `POST /api/arrange` when you already have a `.3mf` with the correct geometry "
        "and want to repack it (e.g., after hand-editing or re-importing).\n"
        "- Use `POST /api/pack` when starting from STL files that need to be combined into "
        "a 3MF first.\n\n"
        "**Settings preservation:** the input 3MF's `Metadata/project_settings.config` is "
        "passed to OrcaSlicer via `--datadir`, which means OrcaSlicer may load its own "
        "machine profiles from `/config`. The `Metadata/model_settings.config` (extruder/slot "
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
)
async def auto_arrange_3mf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    arrange: bool = Form(True),
    orient: bool = Form(True),
):
    # Fix R1: _safe_filename now guards against None before any attribute access
    try:
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=400, detail="Arrange endpoint only supports .3mf files.")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(ARRANGE_DIR, job_id)
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
        "--datadir", CONFIG_DIR,
        "--export-3mf", out_file,
    ]

    if arrange:
        cmd.extend(["--arrange", "1"])
    if orient:
        cmd.extend(["--orient", "1"])

    cmd.append(in_file)
    logger.info(f"Running arrange command: {' '.join(cmd)}")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=float(ARRANGE_TIMEOUT))
        exit_code = process.returncode

        if exit_code != 0 or not os.path.exists(out_file):
            # Fix R4: don't embed raw slicer output (contains internal paths) in HTTP response
            logger.error(
                f"Arrangement failed. Exit code {exit_code}. Log:\n"
                + (stdout.decode("utf-8", errors="replace") if stdout else "(no output)")
            )
            background_tasks.add_task(cleanup_directory, job_dir)
            raise HTTPException(
                status_code=400,
                detail=f"Slicer auto-arrange process failed (exit code {exit_code}). Check server logs for details.",
            )

        stable_out = os.path.join(ARRANGE_DIR, f"{job_id}_output.3mf")
        shutil.copy2(out_file, stable_out)

        response = FileResponse(
            path=stable_out,
            filename=f"arranged_{safe_name}",
            media_type="application/octet-stream",
        )
        background_tasks.add_task(cleanup_directory, job_dir)
        background_tasks.add_task(cleanup_file, stable_out)
        return response

    except asyncio.TimeoutError:
        background_tasks.add_task(cleanup_directory, job_dir)
        raise HTTPException(status_code=408, detail=f"Slicer arrange execution timed out after {ARRANGE_TIMEOUT} seconds.")
    except HTTPException:
        raise
    except Exception as e:
        background_tasks.add_task(cleanup_directory, job_dir)
        logger.exception("System exception during arrange operation")
        raise HTTPException(status_code=500, detail=f"System error during arrangement: {str(e)}")


@app.post(
    "/api/profiles/upload",
    tags=["profiles"],
    summary="Upload a user profile JSON",
    description=(
        "Upload a flat OrcaSlicer preset JSON into the user config volume at "
        "`/config/user/default/{type}/`. A catalog rebuild is triggered in the background "
        "automatically — wait ~5 s and call `GET /api/profiles` to confirm the profile "
        "appears before using it in a slice job.\n\n"
        "**Requirements for the JSON file:**\n"
        "- Must have a `.json` extension\n"
        "- Must be a *fully-flattened* preset with no `inherits` chain. If the profile "
        "relies on a system base profile, flatten it first using `flatten_profiles.py` "
        "inside the container: `docker exec laminus python /workspace/app/flatten_profiles.py "
        "--profile 'Elegoo Centauri Carbon 0.4 nozzle'`\n"
        "- `type` must be exactly `machine`, `process`, or `filament`\n\n"
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
)
async def upload_profile(
    type: str = Form(...),
    file: UploadFile = File(...),
):
    if type not in ("machine", "process", "filament"):
        raise HTTPException(status_code=400, detail="type must be machine, process, or filament.")

    try:
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not safe_name.endswith(".json"):
        raise HTTPException(status_code=400, detail="Profile file must be a .json file.")

    target_dir = os.path.join(USER_CONFIG_DIR, "default", type)
    os.makedirs(target_dir, exist_ok=True)
    target_file = os.path.join(target_dir, safe_name)
    with open(target_file, "wb") as buffer:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buffer)

    _trigger_catalog_rebuild()

    return {
        "status": "success",
        "message": f"Profile uploaded to {type}/{safe_name}. Catalog rebuild started.",
        "filename": safe_name,
    }


@app.get(
    "/api/health",
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
        "- `config_mounted` — whether `/config` exists (volume mount check)\n"
        "- `system_profiles_available` — whether the OrcaSlicer AppImage's profiles "
        "directory is present (required for catalog build)\n"
        "- `catalog_loaded` — `true` when the profile catalog is fully built and ready; "
        "`false` during initial startup or rebuild\n"
        "- `catalog_building` — `true` while a catalog rebuild is in progress\n"
        "- `catalog_profile_count` — `{machine: N, process: N, filament: N}` when loaded, "
        "otherwise `null`\n"
        "- `active_jobs` — number of jobs currently in `slicing` status"
    ),
)
async def health_check():
    active = sum(1 for j in list(jobs.values()) if j["status"] == "slicing")
    return {
        "status": "healthy",
        "orcaslicer_installed": os.path.exists("/usr/local/bin/orcaslicer"),
        "orcaslicer_version": _orcaslicer_version,
        "config_mounted": os.path.exists(CONFIG_DIR),
        "system_profiles_available": os.path.isdir(SYSTEM_PROFILES_DIR),
        "catalog_loaded": catalog is not None and catalog.is_built,
        "catalog_building": _catalog_building,
        "catalog_profile_count": catalog.counts if (catalog and catalog.is_built) else None,
        "active_jobs": active,
    }
