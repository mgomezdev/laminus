import os
import shutil
import asyncio
import uuid
import glob
import logging
import json
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orcaslicer-api")

app = FastAPI(
    title="OrcaSlicer CLI Container API",
    description="A lightweight API and Web UI to slice 3D models using OrcaSlicer CLI headlessly.",
    version="1.0.0"
)

# Enable CORS for convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration Directory Paths
CONFIG_DIR = "/config"
USER_CONFIG_DIR = os.path.join(CONFIG_DIR, "user")
DATA_DIR = "/data"
JOBS_DIR = "/tmp/jobs"

# Ensure directories exist
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)

# Pydantic Schemas for validation
class SliceConfig(BaseModel):
    printer: str = Field(..., description="Path or name of the printer preset JSON file.")
    process: str = Field(..., description="Path or name of the process preset JSON file.")
    plate: int = Field(0, description="Build plate ID to slice (0 for all).")
    filaments: Dict[str, str] = Field(..., description="Mapping of extruder slots (1-indexed string keys) to filament preset JSON files.")

# Helper to initialize default config directories if they don't exist
def init_config_directories():
    default_dirs = [
        os.path.join(USER_CONFIG_DIR, "default", "machine"),
        os.path.join(USER_CONFIG_DIR, "default", "process"),
        os.path.join(USER_CONFIG_DIR, "default", "filament"),
    ]
    for d in default_dirs:
        os.makedirs(d, exist_ok=True)
        # Add a README file in each directory to assist the user
        readme_path = os.path.join(d, "README.txt")
        if not os.path.exists(readme_path):
            folder_type = os.path.basename(d)
            with open(readme_path, "w") as f:
                f.write(f"Drop your OrcaSlicer {folder_type} JSON profiles in this directory.\n")
                f.write(f"They will automatically appear in the Web UI / API list of profiles.\n")

init_config_directories()

# Global dict to store job states
jobs: Dict[str, dict] = {}

class JobLogger:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.queue = asyncio.Queue()
        self.logs = []

    def log(self, message: str):
        formatted_message = message.strip()
        if formatted_message:
            self.logs.append(formatted_message)
            self.queue.put_nowait(formatted_message)

    async def get_stream(self):
        # First send historical logs
        for log in self.logs:
            yield f"data: {log}\n\n"
        
        # Then stream new logs in real-time
        while True:
            log = await self.queue.get()
            yield f"data: {log}\n\n"
            if log == "__COMPLETED__" or log.startswith("__FAILED__"):
                break

def find_profiles_in_config() -> dict:
    """Scan /config/user/ recursively for machine, process, and filament profiles."""
    profiles = {
        "machine": [],
        "process": [],
        "filament": []
    }
    
    if not os.path.exists(USER_CONFIG_DIR):
        return profiles

    # Scan the user directory recursively
    for root, dirs, files in os.walk(USER_CONFIG_DIR):
        dirname = os.path.basename(root)
        if dirname in ["machine", "process", "filament"]:
            for file in files:
                if file.endswith(".json") and not file.startswith("."):
                    path = os.path.join(root, file)
                    rel_path = os.path.relpath(path, USER_CONFIG_DIR)
                    name = os.path.splitext(file)[0]
                    # Also determine user folder name (e.g. 'default' or UUID)
                    parts = rel_path.split(os.sep)
                    user_sub = parts[0] if len(parts) > 1 else "default"
                    
                    profiles[dirname].append({
                        "name": f"{user_sub} / {name}" if user_sub != "default" else name,
                        "filename": file,
                        "rel_path": rel_path,
                        "full_path": path
                    })
    return profiles

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Serves the dashboard index page."""
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Frontend template index.html not found.")
    
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

@app.get("/api/profiles")
async def get_profiles():
    """Endpoint to list available configuration profiles."""
    init_config_directories() # Ensure directories exist
    return find_profiles_in_config()

async def run_orcaslicer_task(
    job_id: str,
    input_file_path: str,
    output_dir: str,
    machine_profile: str,
    process_profile: str,
    filament_mapping: Dict[str, str],
    plate_id: int = 0
):
    job = jobs[job_id]
    job_logger = job["logger"]
    job["status"] = "slicing"
    
    job_logger.log(f"Starting slice job for model: {os.path.basename(input_file_path)}")
    job_logger.log(f"Output directory: {output_dir}")

    # Build the command line arguments
    cmd = [
        "xvfb-run",
        "-a",
        "--server-args=-screen 0 1024x768x24",
        "orcaslicer",
        "--datadir", CONFIG_DIR,
        "--slice", str(plate_id),
        "--outputdir", output_dir
    ]

    # Find configuration profiles full path
    profiles = find_profiles_in_config()
    
    settings_to_load = []
    
    # Resolve printer preset path
    match_printer = next((p for p in profiles["machine"] if p["rel_path"] == machine_profile or p["name"] == machine_profile or p["filename"] == machine_profile), None)
    if match_printer:
        settings_to_load.append(match_printer["full_path"])
        job_logger.log(f"Loading printer profile: {match_printer['name']}")
    else:
        job_logger.log(f"WARNING: Printer profile '{machine_profile}' not found.")

    # Resolve process preset path
    match_process = next((p for p in profiles["process"] if p["rel_path"] == process_profile or p["name"] == process_profile or p["filename"] == process_profile), None)
    if match_process:
        settings_to_load.append(match_process["full_path"])
        job_logger.log(f"Loading process profile: {match_process['name']}")
    else:
        job_logger.log(f"WARNING: Process profile '{process_profile}' not found.")

    if settings_to_load:
        cmd.extend(["--load-settings", ";".join(settings_to_load)])

    # Resolve filament mappings (slot -> preset)
    slot_paths = {}
    for slot_str, fil_name in filament_mapping.items():
        try:
            slot_num = int(slot_str)
        except ValueError:
            job_logger.log(f"WARNING: Invalid filament slot key: '{slot_str}'. Must be an integer.")
            continue
            
        match_fil = next((p for p in profiles["filament"] if p["rel_path"] == fil_name or p["name"] == fil_name or p["filename"] == fil_name), None)
        if match_fil:
            slot_paths[slot_num] = match_fil["full_path"]
            job_logger.log(f"Mapping slot {slot_num} to filament: {match_fil['name']}")
        else:
            job_logger.log(f"WARNING: Filament profile '{fil_name}' for slot {slot_num} not found.")

    # Sort slots and build G-code load list, filling in any gaps
    if slot_paths:
        max_slot = max(slot_paths.keys())
        filament_files = []
        fallback_path = list(slot_paths.values())[0] # use the first matched filament as fallback
        
        for slot in range(1, max_slot + 1):
            path = slot_paths.get(slot, fallback_path)
            filament_files.append(path)
            
        # Semicolon joined list maps to slot 1, slot 2, etc.
        cmd.extend(["--load-filaments", ";".join(filament_files)])
        job_logger.log(f"Prepared filaments loading sequence: {filament_files}")

    # Append input model
    cmd.append(input_file_path)
    job_logger.log(f"Executing command: {' '.join(cmd)}")

    try:
        # Start async subprocess
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        # Read line by line
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            job_logger.log(text)

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
    config: str = Form(..., description="JSON-string matching SliceConfig schema")
):
    """Starts a slicing job using a raw file upload and a combined JSON config string."""
    try:
        config_data = SliceConfig.model_validate_json(config)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Configuration parameter 'config' validation failed. Must be a valid SliceConfig JSON. Error: {str(e)}"
        )

    job_id = str(uuid.uuid4())
    
    # Define directories
    job_dir = os.path.join(JOBS_DIR, job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    
    # Save input file
    input_file_path = os.path.join(input_dir, file.filename)
    with open(input_file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Create logger for the job
    job_logger = JobLogger(job_id)
    
    # Save job details
    jobs[job_id] = {
        "id": job_id,
        "status": "pending",
        "input_file": input_file_path,
        "output_dir": output_dir,
        "sliced_file": None,
        "error": None,
        "logger": job_logger
    }
    
    # Run the slicing task in the background
    background_tasks.add_task(
        run_orcaslicer_task,
        job_id=job_id,
        input_file_path=input_file_path,
        output_dir=output_dir,
        machine_profile=config_data.printer,
        process_profile=config_data.process,
        filament_mapping=config_data.filaments,
        plate_id=config_data.plate
    )
    
    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Slicing job started in background."
    }

@app.get("/api/slice/status/{job_id}")
async def get_job_status(job_id: str):
    """Query status of a specific slicing job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
        
    job = jobs[job_id]
    return {
        "job_id": job["id"],
        "status": job["status"],
        "sliced_file": os.path.basename(job["sliced_file"]) if job["sliced_file"] else None,
        "error": job["error"]
    }

@app.get("/api/slice/logs/{job_id}")
async def get_job_logs(job_id: str):
    """Stream execution logs for a slicing job in real-time using Server-Sent Events (SSE)."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
        
    job = jobs[job_id]
    return StreamingResponse(
        job["logger"].get_stream(),
        media_type="text/event-stream"
    )

@app.get("/api/slice/download/{job_id}")
async def download_sliced_file(job_id: str):
    """Downloads the G-code or 3MF generated by the slicing job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
        
    job = jobs[job_id]
    if job["status"] != "completed" or not job["sliced_file"]:
        raise HTTPException(status_code=400, detail=f"Slicing job is not complete. Current status: {job['status']}")
        
    file_path = job["sliced_file"]
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Sliced file was not found on disk.")
        
    filename = os.path.basename(file_path)
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/octet-stream"
    )

# Cleanup task for arrangement
def cleanup_directory(directory_path: str):
    if os.path.exists(directory_path):
        shutil.rmtree(directory_path)
        logger.info(f"Cleaned up arrangement temp folder: {directory_path}")

@app.post("/api/arrange")
async def auto_arrange_3mf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    arrange: bool = Form(True),
    orient: bool = Form(True)
):
    """Accepts a 3MF project file, runs auto-arrange/orient layout solving, and returns the arranged 3MF file directly."""
    if not file.filename.endswith(".3mf") and not file.filename.endswith(".3MF"):
        raise HTTPException(status_code=400, detail="Arrange endpoint only supports .3mf files.")
        
    job_id = str(uuid.uuid4())
    job_dir = os.path.join("/tmp/arrange", job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    
    in_file = os.path.join(input_dir, file.filename)
    out_file = os.path.join(output_dir, f"arranged_{file.filename}")
    
    # Save uploaded file
    with open(in_file, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Build command
    cmd = [
        "xvfb-run",
        "-a",
        "--server-args=-screen 0 1024x768x24",
        "orcaslicer",
        "--datadir", CONFIG_DIR,
        "--export-3mf", out_file
    ]
    
    if arrange:
        cmd.extend(["--arrange", "1"])
    if orient:
        cmd.extend(["--orient", "1"])
        
    cmd.append(in_file)
    logger.info(f"Running arrange command: {' '.join(cmd)}")
    
    try:
        # Run process synchronously (with a timeout of 30 seconds for safety)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=35.0)
        exit_code = process.returncode
        
        if exit_code != 0 or not os.path.exists(out_file):
            error_log = stdout.decode("utf-8", errors="replace") if stdout else "Unknown error"
            logger.error(f"Arrangement failed. Exit code {exit_code}. Log:\n{error_log}")
            raise HTTPException(
                status_code=400,
                detail=f"Slicer auto-arrange process failed. Slicer Output: {error_log[-500:]}"
            )
            
        # Success - Stream the file back and add cleanup job to background tasks
        response = FileResponse(
            path=out_file,
            filename=f"arranged_{file.filename}",
            media_type="application/octet-stream"
        )
        background_tasks.add_task(cleanup_directory, job_dir)
        return response
        
    except asyncio.TimeoutError:
        background_tasks.add_task(cleanup_directory, job_dir)
        raise HTTPException(status_code=408, detail="Slicer arrange execution timed out after 35 seconds.")
    except HTTPException:
        background_tasks.add_task(cleanup_directory, job_dir)
        raise
    except Exception as e:
        background_tasks.add_task(cleanup_directory, job_dir)
        logger.exception("System exception during arrange operation")
        raise HTTPException(status_code=500, detail=f"System error during arrangement: {str(e)}")

@app.post("/api/profiles/upload")
async def upload_profile(
    type: str = Form(...),
    file: UploadFile = File(...)
):
    """Uploads a preset profile JSON file directly to the config directory."""
    if type not in ["machine", "process", "filament"]:
        raise HTTPException(status_code=400, detail="Invalid profile type. Must be machine, process, or filament.")
        
    if not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="Profile file must be a JSON file.")
        
    target_dir = os.path.join(USER_CONFIG_DIR, "default", type)
    os.makedirs(target_dir, exist_ok=True)
    
    target_file = os.path.join(target_dir, file.filename)
    with open(target_file, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    return {
        "status": "success",
        "message": f"Profile uploaded successfully to {type}/{file.filename}",
        "filename": file.filename
    }

@app.get("/api/health")
async def health_check():
    """Simple check to verify the API container is running and responsive."""
    return {
        "status": "healthy",
        "orcaslicer_installed": os.path.exists("/usr/local/bin/orcaslicer"),
        "config_mounted": os.path.exists(CONFIG_DIR)
    }
