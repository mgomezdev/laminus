import sys
import os
import shutil
import asyncio
import time
import logging
import json
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
from typing import Dict, Optional
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.profile_catalog import ProfileCatalog
# Not used in this module: app/routes/packing.py calls it as `main._inject_stls_into_3mf` so a test can replace it here.
from app.stl_to_3mf import inject_stls_into_3mf as _inject_stls_into_3mf  # noqa: F401

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("laminus")


def _form_default_true(value) -> bool:
    # Recent FastAPI coerces an omitted bool Form field -- even `Optional[str] = Form(None)`
    # and `bool = Form(True)` -- to False (HTML-checkbox semantics). True-default flags
    # therefore take `Optional[str] = Form(None)` and are parsed here; omitted means True.
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def _form_default_false(value) -> bool:
    """Opt-in flag: omitted means False; "true"/"1"/"yes"/"on" turn it on."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


GEOMETRY_ONLY_RETRY_DESC = (
    "Opt-in fallback, default `false`. When `true` and the first slice fails, retry once with "
    "`Metadata/model_settings.config` removed. The retry DISCARDS every per-object and per-part "
    "override (walls, infill, supports, brim, modifiers, layer ranges, extruder assignment) and "
    "the plate settings, so the result can differ from the project; the job log says so when it "
    "happens."
)


# CONFIG_DIR / DATA_DIR are env-overridable so the e2e suite can run the real app as a
# non-root user; in the container they stay at their mount points.
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
USER_CONFIG_DIR = os.environ.get("USER_CONFIG_DIR", os.path.join(CONFIG_DIR, "user"))
# The datadir passed to the OrcaSlicer CLI itself (--datadir). Set by entrypoint.sh to
# a disposable scratch path, with only user/ and plugins/ symlinked back into the
# durable CONFIG_DIR — Orca also writes cache/ and log/ under whatever --datadir it's
# given, and neither belongs in a volume that gets backed up. Falls back to CONFIG_DIR
# so local (non-Docker, non-entrypoint.sh) dev keeps working.
ORCA_DATADIR = os.environ.get("ORCA_DATADIR", CONFIG_DIR)
DATA_DIR = os.environ.get("DATA_DIR", "/data")
JOBS_DIR = os.environ.get("JOBS_DIR", "/tmp/jobs")
ARRANGE_DIR = os.environ.get("ARRANGE_DIR", "/tmp/arrange")

JOB_TTL = int(os.environ.get("JOB_TTL_SECONDS", "3600"))
JOB_SWEEP_INTERVAL = int(os.environ.get("JOB_SWEEP_INTERVAL_SECONDS", "300"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "4"))
THUMBNAIL_TIMEOUT = 120

SYSTEM_PROFILES_DIR = os.environ.get("SYSTEM_PROFILES_DIR", "/opt/orcaslicer/resources/profiles")
# entrypoint.sh writes the extracted AppImage's version to <install dir>/.version
ORCA_VERSION_FILE = os.environ.get("ORCA_VERSION_FILE", "/opt/orcaslicer/.version")
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT_SECONDS", "600"))
ARRANGE_TIMEOUT = int(os.environ.get("ARRANGE_TIMEOUT_SECONDS", "120"))
JOB_LOG_MAX_LINES = int(os.environ.get("JOB_LOG_MAX_LINES", "2000"))

# The dashboard page (app/routes/system.py serves it). Resolved here because it sits next to this file.
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

catalog: Optional[ProfileCatalog] = None
_catalog_building: bool = False
_orcaslicer_version: Optional[str] = None
_catalog_task: Optional[asyncio.Task] = None
# Template cache: maps (machine_uuid, process_uuid, filament_uuids_key) → 3MF bytes
_template_cache: Dict[str, bytes] = {}
PACK_MAX_INSTANCES = 500  # total copies across all STLs in one /api/v1/pack call


# Fix R6: lifespan context runs startup/shutdown logic and background eviction task
@asynccontextmanager
async def lifespan(app: FastAPI):
    global catalog, _catalog_building, _orcaslicer_version, _catalog_task
    for d in (CONFIG_DIR, DATA_DIR, JOBS_DIR, ARRANGE_DIR):
        os.makedirs(d, exist_ok=True)
    init_config_directories()
    _load_jobs_on_startup()
    _load_history_on_startup()
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


def _trigger_catalog_rebuild(force: bool = False) -> None:
    global _catalog_task, _catalog_building
    if not _catalog_building:
        _catalog_building = True
        _catalog_task = asyncio.create_task(_build_catalog(force=force))


async def _build_catalog(force: bool = False):
    """Verify inheritance across the profile tree and build the catalog dictionary.

    This is the eager pass: every profile's `inherits` chain is walked and validated and
    the display fields Themis lists are computed, so callers get the catalog without
    waiting on a scan. The fully-merged preset is deliberately *not* retained — see
    `ProfileCatalog.resolved()`, which materialises it lazily at slice time so an updated
    vendor base rolls through without any reflattening step.

    `force=True` skips the cache read so a manual rescan re-verifies even when nothing on
    disk appears to have changed.
    """
    global catalog, _catalog_building, _template_cache
    try:
        cache_key = await asyncio.to_thread(_catalog_cache_key)
        cached = None if force else await asyncio.to_thread(
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
        "1. **`GET /api/v1/health`** — wait until `catalog_loaded: true` before making any other calls.\n"
        "2. **`GET /api/v1/profiles`** — discover machine / process / filament UUIDs for your printer "
        "and material. Filter by `manufacturer`, `model`, and `nozzle` to narrow results.\n"
        "3. **`POST /api/v1/jobs`** — upload the model (`.3mf` or `.stl`) with the resolved "
        "UUIDs. The 202 response contains an `id`.\n"
        "4. **`GET /api/v1/jobs/{job_id}`** — poll every 2–5 s until `status` is `completed` "
        "or `failed`.\n"
        "5. **`GET /api/v1/jobs/{job_id}/artifact`** — retrieve the GCode (or `.3mf` when "
        "`export_3mf` was requested). Downloading removes the artifact; the job record remains.\n\n"
        "Alternatively, use **`POST /api/v1/slice/prepared`** when the 3MF already embeds print "
        "settings from a previous OrcaSlicer session — no profile resolution is needed.\n\n"
        "## Packing / arrangement\n\n"
        "Use **`POST /api/v1/pack`** to distribute STL geometry across build plates before slicing. "
        "Three modes: supply a template `.3mf`, profile UUIDs (preferred — catalog-aware), or "
        "explicit bed dimensions. The packed `.3mf` is returned directly (synchronous, up to "
        "120 s).\n\n"
        "## Error conventions\n\n"
        "- **422** — caller-side validation failure (bad UUIDs, missing required fields, etc.).\n"
        "- **503** — Laminus is not yet ready; the catalog is still building. "
        "Retry after `GET /api/v1/health` reports `catalog_loaded: true`.\n"
        "- **400 / 408 / 500** — OrcaSlicer-side failures; check server logs for details."
    ),
    version="0.12.0",
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "health",
            "description": (
                "Service readiness and version information. Always check `/api/v1/health` first — "
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
                "Use `POST /api/v1/pack` to combine multiple STLs into a single multi-plate `.3mf`, "
                "and `POST /api/v1/arrange` to re-pack an existing `.3mf`."
            ),
        },
        {
            "name": "jobs",
            "description": (
                "Job history across the lifetime of the container — every slice job ever "
                "created, including ones already evicted from the active `jobs` table. "
                "Backs the Web UI's status/history view."
            ),
        },
        {
            "name": "maintenance",
            "description": (
                "On-demand disk cleanup for temp files left behind by failed or interrupted "
                "slice/arrange/pack runs (e.g., after a container crash mid-request)."
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

# job_ids of in-flight /api/v1/pack, /api/v1/arrange, /api/v1/slice/thumbnail requests. These
# are synchronous requests tracked in no other registry, so POST /api/v1/cleanup (with
# min_age_seconds=0) would otherwise delete their scratch dirs / stable output copies
# mid-write. _scan_stale_temp excludes any ARRANGE_DIR entry whose name contains one
# of these ids, regardless of age.
_inflight_arrange_ids: set[str] = set()


@contextmanager
def _track_inflight_arrange(job_id: str):
    _inflight_arrange_ids.add(job_id)
    try:
        yield
    finally:
        _inflight_arrange_ids.discard(job_id)


_JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
# Deliberately NOT under DATA_DIR: this is a rebuildable cache (keyed by ORCA_VERSION +
# vendor bundle signature + user profile mtimes — see _catalog_cache_key), not durable
# state. Keeping it off the durable volume means DATA_DIR only ever holds jobs.json and
# job_history.json, both worth backing up in full.
_CATALOG_CACHE_FILE = "/tmp/laminus_catalog_cache.json"


def _vendor_bundle_signature() -> list[tuple]:
    """Signature of the system (vendor) profile tree.

    ORCA_VERSION alone is not sufficient: when SYSTEM_PROFILES_DIR is bind-mounted from
    a host OrcaSlicer install, Orca updates its vendor bundles independently of the
    application version, and a key that ignores them serves stale resolved presets
    indefinitely.

    Only the top-level `<Vendor>.json` bundle files are inspected, not the whole tree —
    OrcaSlicer ships vendor profiles as whole versioned bundles, and stat()-ing several
    thousand nested files is slow precisely where this matters most (a Docker bind mount
    on Windows/WSL2).
    """
    sig: list[tuple] = []
    if not os.path.isdir(SYSTEM_PROFILES_DIR):
        return sig
    try:
        names = os.listdir(SYSTEM_PROFILES_DIR)
    except OSError:
        return sig
    for fn in names:
        if not fn.endswith(".json"):
            continue
        p = os.path.join(SYSTEM_PROFILES_DIR, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        version = ""
        try:
            with open(p, encoding="utf-8") as f:
                version = json.load(f).get("version", "")
        except (OSError, ValueError):
            pass
        sig.append((fn, version, st.st_mtime, st.st_size))
    sig.sort()
    return sig


def _catalog_cache_key() -> str:
    """Hash of ORCA_VERSION + the vendor bundle signature + sorted (relpath, mtime, size)
    for the user config dir. Any preset change on either side voids the cache, forcing a
    fresh inheritance verification and catalog rebuild."""
    import hashlib
    h = hashlib.md5(os.environ.get("ORCA_VERSION", "").encode())
    for entry in _vendor_bundle_signature():
        h.update(str(entry).encode())
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
        try:
            cutoff = time.monotonic() - JOB_TTL
            stale = [jid for jid, j in list(jobs.items()) if j.get("created_at", 0) < cutoff]
            for jid in stale:
                j = jobs.pop(jid, None)
                if j:
                    job_dir = os.path.join(JOBS_DIR, jid)
                    await asyncio.to_thread(cleanup_directory, job_dir)
                    _history_upsert(jid, status=j.get("status"), error=j.get("error"), evicted=True, updated_at=time.time())
                    logger.info(f"Evicted stale job {jid}")
        except Exception:
            # A single bad sweep must never kill the loop — jobs/JOBS_DIR would then
            # grow unbounded for the rest of the process lifetime.
            logger.exception("Stale-job sweep iteration failed")


# --- Job history: a longer-lived record of every job, independent of the active `jobs` dict.
# Backs GET /api/v1/jobs (status + history view in the Web UI). Entries persist across the job's
# eviction from `jobs` (download or TTL sweep) so the UI can still show what happened.
job_history: "OrderedDict[str, dict]" = OrderedDict()
_HISTORY_FILE = os.path.join(DATA_DIR, "job_history.json")
JOB_HISTORY_LIMIT = int(os.environ.get("JOB_HISTORY_LIMIT", "200"))


def _history_upsert(job_id: str, **fields) -> None:
    rec = job_history.get(job_id)
    if rec is None:
        rec = {"id": job_id}
        job_history[job_id] = rec
    rec.update(fields)
    job_history.move_to_end(job_id)
    while len(job_history) > JOB_HISTORY_LIMIT:
        job_history.popitem(last=False)
    _save_history()


def _save_history() -> None:
    """Persist job history to disk (best-effort)."""
    try:
        tmp = _HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(job_history.values()), f)
        os.replace(tmp, _HISTORY_FILE)
    except OSError:
        pass


def _load_history_on_startup() -> None:
    try:
        with open(_HISTORY_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    for rec in saved:
        jid = rec.get("id")
        if not jid:
            continue
        if rec.get("status") in ("pending", "slicing"):
            rec["status"] = "failed"
            rec["error"] = "Laminus restarted while job was in progress"
            rec["evicted"] = True
        job_history[jid] = rec


class JobLogger:
    def __init__(self, job_id: str, max_lines: int = JOB_LOG_MAX_LINES):
        self.job_id = job_id
        self._logs: deque[str] = deque(maxlen=max_lines)
        self._dropped = 0  # count of messages evicted off the front of _logs
        self._total = 0  # total messages ever logged, i.e. the next cursor value
        self._new_entry = asyncio.Event()
        self._done = False

    def log(self, message: str):
        msg = message.strip()
        if not msg:
            return
        if len(self._logs) == self._logs.maxlen:
            self._dropped += 1
        self._logs.append(msg)
        self._total += 1
        self._new_entry.set()
        if msg == "__COMPLETED__" or msg.startswith("__FAILED__"):
            self._done = True

    async def get_stream(self):
        cursor = 0
        warned_dropped = False
        while True:
            if cursor < self._dropped:
                if not warned_dropped:
                    yield f"data: __DROPPED__: {self._dropped} earlier line(s) omitted\n\n"
                    warned_dropped = True
                cursor = self._dropped
            while cursor < self._total:
                msg = self._logs[cursor - self._dropped]
                cursor += 1
                yield f"data: {msg}\n\n"
                if msg == "__COMPLETED__" or msg.startswith("__FAILED__"):
                    return
            if self._done:
                return
            self._new_entry.clear()
            await self._new_entry.wait()


# ---- neutral (model / nozzle-variant) profile API --------------------------------
# Answered purely from the catalog, so it works with the engine down: while the live
# catalog is still (re)building, a previously cached one is served with `stale: true`.

_stale_catalog: Optional[ProfileCatalog] = None


def _neutral_catalog() -> tuple[Optional[ProfileCatalog], bool]:
    """(catalog, stale). Falls back to the on-disk cache, whatever its key, when no live
    catalog exists yet; (None, False) when there is nothing to serve at all."""
    global _stale_catalog
    if catalog is not None:
        _stale_catalog = None
        return catalog, _catalog_building
    if _stale_catalog is None:
        _stale_catalog = ProfileCatalog.load_from_cache(
            _CATALOG_CACHE_FILE, None, SYSTEM_PROFILES_DIR, USER_CONFIG_DIR,
        )
    return (_stale_catalog, True) if _stale_catalog is not None else (None, False)


def _neutral_json(cat: ProfileCatalog, stale: bool, body: dict, status_code: int = 200) -> JSONResponse:
    version = cat.neutral.version
    payload = {"catalog_version": version, **body}
    if stale:
        payload["stale"] = True
    return JSONResponse(status_code=status_code, content=payload, headers={"X-Catalog-Version": version})


def _catalog_unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, content={"error": "catalog_unavailable"})


def cleanup_directory(path: str):
    try:
        if os.path.exists(path):
            shutil.rmtree(path)
            logger.info(f"Cleaned up temp folder: {path}")
    except OSError:
        logger.warning(f"Failed to clean up temp folder: {path}", exc_info=True)


def cleanup_file(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


# ---- routes -------------------------------------------------------------------------------------------------------
# The handlers live in app/routes/, one module per area. They are imported here, after everything above exists,
# because each reads this module's state and helpers as `main.<name>` at call time. Their public
# and private names are re-exported so `app.main.<name>` keeps working for api_v1, the tests and each other.
# Importing a module registers its routes on `app`. Order matters twice: a module may use an earlier one's names at
# import time, and routes match (and appear in openapi.json) in this order.
from app.routes.profiles import (  # noqa: E402,F401,I001
    MergedConfigRequest,  # noqa: F401
    get_merged_config,  # noqa: F401
    get_broken_profiles,  # noqa: F401
    NeutralModelQuery,  # noqa: F401
    _NEUTRAL_RESPONSES,  # noqa: F401
    suggest_machines,  # noqa: F401
    model_variants,  # noqa: F401
    machine_presets,  # noqa: F401
    machine_supports,  # noqa: F401
    upload_profile,  # noqa: F401
    rescan_profiles,  # noqa: F401
)
from app.routes.slicing import (  # noqa: E402,F401,I001
    _kill_process_group,  # noqa: F401
    _stream_subprocess_output,  # noqa: F401
    _strip_model_settings,  # noqa: F401
    _sliced_plate_gcode_error,  # noqa: F401
    run_orcaslicer_task,  # noqa: F401
    start_slice,  # noqa: F401
    slice_prepared,  # noqa: F401
    slice_thumbnail,  # noqa: F401
    download_sliced_file,  # noqa: F401
    _evict_job,  # noqa: F401
)
from app.routes.projects import (  # noqa: E402,F401,I001
    auto_arrange_3mf,  # noqa: F401
    list_3mf_objects,  # noqa: F401
    _embed_manifest,  # noqa: F401
    _oracle_arrange_and_transplant,  # noqa: F401
    _orca_arrange,  # noqa: F401
    preview_3mf_objects,  # noqa: F401
    arrange_subset,  # noqa: F401
    _Retarget,  # noqa: F401
    _resolve_retarget,  # noqa: F401
    arrange_merge,  # noqa: F401
    measure_project,  # noqa: F401
)
from app.routes.packing import (  # noqa: E402,F401,I001
    _build_bed_template_bytes,  # noqa: F401
    _items_error,  # noqa: F401
    _parse_pack_items,  # noqa: F401
    pack_stls,  # noqa: F401
    _mixed_selection,  # noqa: F401
    _mixed_project,  # noqa: F401
    pack_next,  # noqa: F401
    pack_mixed,  # noqa: F401
)
from app.routes.system import (  # noqa: E402,F401,I001
    get_dashboard,  # noqa: F401
    _orca_version,  # noqa: F401
    health_check,  # noqa: F401
    _dir_size,  # noqa: F401
    _scan_stale_temp,  # noqa: F401
    cleanup_temp,  # noqa: F401
    test_known_profile,  # noqa: F401
)

from app import api_v1 as _api_v1  # noqa: E402  (installs /api/v1 once every legacy route exists)
_api_v1.install(app, sys.modules[__name__])
