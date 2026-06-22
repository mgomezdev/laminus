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
from pydantic import BaseModel, Field, field_validator
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.profile_catalog import ProfileCatalog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orcaslicer-api")

CONFIG_DIR = "/config"
USER_CONFIG_DIR = os.path.join(CONFIG_DIR, "user")
DATA_DIR = "/data"
JOBS_DIR = "/tmp/jobs"
ARRANGE_DIR = "/tmp/arrange"

JOB_TTL = 3600             # Fix R6: seconds after creation before a completed job is evicted
JOB_SWEEP_INTERVAL = 300   # how often the eviction sweep runs

SYSTEM_PROFILES_DIR = os.environ.get("SYSTEM_PROFILES_DIR", "/opt/orcaslicer/resources/profiles")
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT_SECONDS", "600"))

catalog: Optional[ProfileCatalog] = None
_catalog_building: bool = False
_orcaslicer_version: Optional[str] = None
_catalog_task: Optional[asyncio.Task] = None


# Fix R6: lifespan context runs startup/shutdown logic and background eviction task
@asynccontextmanager
async def lifespan(app: FastAPI):
    global catalog, _catalog_building, _orcaslicer_version, _catalog_task
    for d in (CONFIG_DIR, DATA_DIR, JOBS_DIR, ARRANGE_DIR):
        os.makedirs(d, exist_ok=True)
    init_config_directories()
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


async def _build_catalog():
    global catalog, _catalog_building
    try:
        cat = ProfileCatalog(system_dir=SYSTEM_PROFILES_DIR, user_dir=USER_CONFIG_DIR)
        await asyncio.to_thread(cat.build)
        catalog = cat
        logger.info("Profile catalog ready: %s", cat.counts)
    except Exception:
        logger.exception("Catalog build failed")
    finally:
        _catalog_building = False


app = FastAPI(
    title="OrcaSlicer CLI Container API",
    description="A lightweight API and Web UI to slice 3D models using OrcaSlicer CLI headlessly.",
    version="1.0.0",
    lifespan=lifespan,
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


class SliceConfig(BaseModel):
    printer: str = Field(..., description="Path or name of the printer preset JSON file.")
    process: str = Field(..., description="Path or name of the process preset JSON file.")
    plate: int = Field(0, description="Build plate ID to slice (0 for all).")
    filaments: Dict[str, str] = Field(
        ..., description="Mapping of extruder slots (1-indexed string keys) to filament preset JSON files."
    )

    @field_validator("filaments")
    @classmethod
    def filaments_not_empty(cls, v: Dict[str, str]) -> Dict[str, str]:
        if not v:
            raise ValueError("filaments must contain at least one slot mapping.")
        # Fix R2: validate that all slot keys are positive integers
        for key in v:
            try:
                slot = int(key)
            except ValueError:
                raise ValueError(f"Filament slot key '{key}' must be an integer.")
            if slot < 1:
                raise ValueError(f"Filament slot key '{key}' must be >= 1.")
        return v


def init_config_directories():
    default_dirs = [
        os.path.join(USER_CONFIG_DIR, "default", "machine"),
        os.path.join(USER_CONFIG_DIR, "default", "process"),
        os.path.join(USER_CONFIG_DIR, "default", "filament"),
    ]
    for d in default_dirs:
        os.makedirs(d, exist_ok=True)
        readme_path = os.path.join(d, "README.txt")
        if not os.path.exists(readme_path):
            folder_type = os.path.basename(d)
            with open(readme_path, "w") as f:
                f.write(f"Drop your OrcaSlicer {folder_type} JSON profiles in this directory.\n")
                f.write("They will automatically appear in the Web UI / API list of profiles.\n")


jobs: Dict[str, dict] = {}


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


def find_profiles_in_config() -> dict:
    """Blocking sync — callers must wrap with asyncio.to_thread in async context."""
    profiles = {"machine": [], "process": [], "filament": []}
    if not os.path.exists(USER_CONFIG_DIR):
        return profiles
    for root, dirs, files in os.walk(USER_CONFIG_DIR):
        dirname = os.path.basename(root)
        if dirname in ("machine", "process", "filament"):
            for file in files:
                if file.endswith(".json") and not file.startswith("."):
                    path = os.path.join(root, file)
                    rel_path = os.path.relpath(path, USER_CONFIG_DIR)
                    name = os.path.splitext(file)[0]
                    parts = rel_path.split(os.sep)
                    user_sub = parts[0] if len(parts) > 1 else "default"
                    profiles[dirname].append({
                        "name": f"{user_sub} / {name}" if user_sub != "default" else name,
                        "filename": file,
                        "rel_path": rel_path,
                        "full_path": path,
                    })
    return profiles


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Frontend template index.html not found.")
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


@app.get("/api/profiles")
async def get_profiles(
    manufacturer: Optional[str] = None,
    model: Optional[str] = None,
    nozzle: Optional[str] = None,
    refresh: bool = False,
):
    if refresh and not _catalog_building:
        global _catalog_task, _catalog_building
        _catalog_building = True
        _catalog_task = asyncio.create_task(_build_catalog())
    if catalog is None or not catalog.is_built:
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


@app.get("/api/profiles/{profile_uuid}")
async def get_profile_detail(profile_uuid: str):
    if catalog is None or not catalog.is_built:
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


async def run_orcaslicer_task(
    job_id: str,
    input_file_path: str,
    output_dir: str,
    machine_profile: str,
    process_profile: str,
    filament_mapping: Dict[str, str],
    plate_id: int = 0,
):
    job = jobs[job_id]
    job_logger = job["logger"]
    job["status"] = "slicing"

    job_logger.log(f"Starting slice job for model: {os.path.basename(input_file_path)}")
    job_logger.log(f"Output directory: {output_dir}")

    cmd = [
        "xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
        "orcaslicer",
        "--datadir", CONFIG_DIR,
        "--slice", str(plate_id),
        "--outputdir", output_dir,
    ]

    # Fix R7: find_profiles_in_config uses blocking os.walk — run in thread pool
    profiles = await asyncio.to_thread(find_profiles_in_config)
    settings_to_load = []

    match_printer = next(
        (p for p in profiles["machine"]
         if p["rel_path"] == machine_profile or p["name"] == machine_profile or p["filename"] == machine_profile),
        None,
    )
    if match_printer:
        settings_to_load.append(match_printer["full_path"])
        job_logger.log(f"Loading printer profile: {match_printer['name']}")
    else:
        job["status"] = "failed"
        job["error"] = f"Printer profile '{machine_profile}' not found."
        job_logger.log(f"ERROR: Printer profile '{machine_profile}' not found.")
        job_logger.log("__FAILED__: Printer profile not found")
        return

    match_process = next(
        (p for p in profiles["process"]
         if p["rel_path"] == process_profile or p["name"] == process_profile or p["filename"] == process_profile),
        None,
    )
    if match_process:
        settings_to_load.append(match_process["full_path"])
        job_logger.log(f"Loading process profile: {match_process['name']}")
    else:
        job["status"] = "failed"
        job["error"] = f"Process profile '{process_profile}' not found."
        job_logger.log(f"ERROR: Process profile '{process_profile}' not found.")
        job_logger.log("__FAILED__: Process profile not found")
        return

    if settings_to_load:
        cmd.extend(["--load-settings", ";".join(settings_to_load)])

    slot_paths: Dict[int, str] = {}
    for slot_str, fil_name in filament_mapping.items():
        slot_num = int(slot_str)  # already validated >= 1 by SliceConfig.filaments_not_empty
        match_fil = next(
            (p for p in profiles["filament"]
             if p["rel_path"] == fil_name or p["name"] == fil_name or p["filename"] == fil_name),
            None,
        )
        if match_fil:
            slot_paths[slot_num] = match_fil["full_path"]
            job_logger.log(f"Mapping slot {slot_num} to filament: {match_fil['name']}")
        else:
            job_logger.log(f"WARNING: Filament profile '{fil_name}' for slot {slot_num} not found.")

    if slot_paths:
        max_slot = max(slot_paths.keys())
        fallback_path = slot_paths.get(1) or next(iter(slot_paths.values()))
        filament_files = [slot_paths.get(slot, fallback_path) for slot in range(1, max_slot + 1)]
        cmd.extend(["--load-filaments", ";".join(filament_files)])
        job_logger.log(f"Prepared filaments loading sequence: {filament_files}")

    cmd.append(input_file_path)
    job_logger.log(f"Executing command: {' '.join(cmd)}")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            await asyncio.wait_for(_stream_subprocess_output(process, job_logger), timeout=SLICE_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            job["status"] = "failed"
            job["error"] = f"Slicer timed out after {SLICE_TIMEOUT}s."
            job_logger.log(f"ERROR: OrcaSlicer timed out after {SLICE_TIMEOUT} seconds.")
            job_logger.log(f"__FAILED__: Timeout after {SLICE_TIMEOUT}s")
            return

        await process.wait()
        exit_code = process.returncode

        if exit_code == 0:
            job_logger.log("OrcaSlicer execution completed successfully.")
            output_files = glob.glob(os.path.join(output_dir, "*"))
            gcode_files = [f for f in output_files if f.endswith((".gcode", ".3mf"))]
            if gcode_files:
                job["status"] = "completed"
                job["sliced_file"] = gcode_files[0]
                job_logger.log(f"Sliced file generated: {os.path.basename(gcode_files[0])}")
                job_logger.log("__COMPLETED__")
            else:
                job["status"] = "failed"
                job["error"] = "No G-code or 3MF output file was generated by the slicer."
                job_logger.log("ERROR: No output files found in output directory.")
                job_logger.log("__FAILED__: Output files missing")
        else:
            job["status"] = "failed"
            job["error"] = f"Slicer process exited with error code {exit_code}."
            job_logger.log(f"ERROR: OrcaSlicer exited with code {exit_code}.")
            job_logger.log(f"__FAILED__: Process exited with code {exit_code}")

    except Exception as e:
        logger.exception("Exception occurred during slicing task")
        job["status"] = "failed"
        job["error"] = str(e)
        job_logger.log(f"SYSTEM ERROR: {str(e)}")
        job_logger.log("__FAILED__: System Exception")


@app.post("/api/slice/start")
async def start_slice(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    config: str = Form(..., description="JSON-string matching SliceConfig schema"),
):
    try:
        config_data = SliceConfig.model_validate_json(config)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Configuration parameter 'config' validation failed. Must be a valid SliceConfig JSON. Error: {str(e)}",
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

    input_file_path = os.path.join(input_dir, safe_name)
    # Fix R8: shutil.copyfileobj is blocking — run in thread pool
    with open(input_file_path, "wb") as buffer:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buffer)

    job_logger = JobLogger(job_id)

    jobs[job_id] = {
        "id": job_id,
        "status": "pending",
        "input_file": input_file_path,
        "output_dir": output_dir,
        "sliced_file": None,
        "error": None,
        "logger": job_logger,
        "created_at": time.monotonic(),  # Fix R6: timestamp for TTL eviction
    }

    background_tasks.add_task(
        run_orcaslicer_task,
        job_id=job_id,
        input_file_path=input_file_path,
        output_dir=output_dir,
        machine_profile=config_data.printer,
        process_profile=config_data.process,
        filament_mapping=config_data.filaments,
        plate_id=config_data.plate,
    )

    return {"job_id": job_id, "status": "pending", "message": "Slicing job started in background."}


@app.get("/api/slice/status/{job_id}")
async def get_job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    job = jobs[job_id]
    return {
        "job_id": job["id"],
        "status": job["status"],
        "sliced_file": os.path.basename(job["sliced_file"]) if job["sliced_file"] else None,
        "error": job["error"],
    }


@app.get("/api/slice/logs/{job_id}")
async def get_job_logs(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    job = jobs[job_id]
    return StreamingResponse(job["logger"].get_stream(), media_type="text/event-stream")


@app.get("/api/slice/download/{job_id}")
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


@app.post("/api/arrange")
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

        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=35.0)
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
        raise HTTPException(status_code=408, detail="Slicer arrange execution timed out after 35 seconds.")
    except HTTPException:
        raise
    except Exception as e:
        background_tasks.add_task(cleanup_directory, job_dir)
        logger.exception("System exception during arrange operation")
        raise HTTPException(status_code=500, detail=f"System error during arrangement: {str(e)}")


@app.post("/api/profiles/upload")
async def upload_profile(
    type: str = Form(...),
    file: UploadFile = File(...),
):
    if type not in ("machine", "process", "filament"):
        raise HTTPException(status_code=400, detail="Invalid profile type. Must be machine, process, or filament.")

    # Fix R1: _safe_filename guards against None before extension check
    try:
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not safe_name.endswith(".json"):
        raise HTTPException(status_code=400, detail="Profile file must be a JSON file.")

    target_dir = os.path.join(USER_CONFIG_DIR, "default", type)
    os.makedirs(target_dir, exist_ok=True)

    target_file = os.path.join(target_dir, safe_name)
    # Fix R8: blocking file write — run in thread pool
    with open(target_file, "wb") as buffer:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buffer)

    return {
        "status": "success",
        "message": f"Profile uploaded successfully to {type}/{safe_name}",
        "filename": safe_name,
    }


@app.get("/api/health")
async def health_check():
    active = sum(1 for j in list(jobs.values()) if j["status"] == "slicing")
    return {
        "status": "healthy",
        "orcaslicer_installed": os.path.exists("/usr/local/bin/orcaslicer"),
        "orcaslicer_version": _orcaslicer_version,
        "config_mounted": os.path.exists(CONFIG_DIR),
        "system_profiles_available": os.path.isdir(SYSTEM_PROFILES_DIR),
        "catalog_loaded": catalog is not None and catalog.is_built,
        "catalog_profile_count": catalog.counts if (catalog and catalog.is_built) else None,
        "active_jobs": active,
    }
