"""Laminus mock server for testing and third-party integration.

Serves a stable, canned profile catalog and completes slice jobs synchronously
(no OrcaSlicer required). Safe to use in any CI pipeline.

Run standalone:
    uvicorn server:app --host 0.0.0.0 --port 5000
Or via Docker:
    docker run -p 5000:5000 ninjabuffalo/laminus-mock:1
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

# Stable UUIDs — guaranteed constant across releases so tests can hardcode them
MACHINE_UUID = "00000000-mock-mach-0004-elegoocc04noz"
PROCESS_UUID = "00000000-mock-proc-016m-optimal00000"
FILAMENT_UUID = "00000000-mock-fila-0000-elegooplaefcc"

_MACHINE = {
    "uuid": MACHINE_UUID,
    "name": "Elegoo Centauri Carbon 0.4 nozzle (mock)",
    "manufacturer": "Elegoo",
    "model": "Centauri Carbon",
    "nozzle": "0.4",
    "nozzle_diameter": 0.4,
    "bed_size_x": 256.0,
    "bed_size_y": 256.0,
    "extruder_count": 1,
    "source": "mock",
    "rel_path": "mock/machine.json",
    "type": "machine",
}
_PROCESS = {
    "uuid": PROCESS_UUID,
    "name": "0.16mm Optimal @Elegoo CC 0.4 nozzle (mock)",
    "layer_height": 0.16,
    "compatible_printers": ["Elegoo Centauri Carbon 0.4 nozzle (mock)"],
    "source": "mock",
    "rel_path": "mock/process.json",
    "type": "process",
}
_FILAMENT = {
    "uuid": FILAMENT_UUID,
    "name": "Elegoo PLA @ECC (mock)",
    "display_name": "Elegoo PLA (mock)",
    "filament_type": "PLA",
    "filament_colour": "#FFFFFF",
    "filament_vendor": "Elegoo",
    "filament_diameter": 1.75,
    "filament_density": 1.24,
    "nozzle_temperature": 220,
    "nozzle_temperature_range_low": 190,
    "nozzle_temperature_range_high": 240,
    "bed_temperature": 60,
    "bed_temperature_initial_layer": 65,
    "compatible_printers": ["Elegoo Centauri Carbon 0.4 nozzle (mock)"],
    "source": "mock",
    "rel_path": "mock/filament.json",
    "type": "filament",
}

_CATALOG: dict[str, dict] = {
    MACHINE_UUID: _MACHINE,
    PROCESS_UUID: _PROCESS,
    FILAMENT_UUID: _FILAMENT,
}

_MERGED_CONFIG = {
    "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    "printable_height": "256",
    "nozzle_diameter": ["0.4"],
    "layer_height": "0.16",
    "filament_type": ["PLA"],
    "nozzle_temperature": ["220"],
    "bed_temperature": ["60"],
    "curr_bed_type": "Cool Plate",
}

_MOCK_GCODE = b"; mock gcode from laminus-mock\nG28\nM109 S220\nG1 X128 Y128 Z0.2 F3000\nM104 S0\nM84\n"

_jobs: dict[str, str] = {}  # job_id → status ("completed")

app = FastAPI(
    title="Laminus",
    description="Mock Laminus OrcaSlicer sidecar — stable canned catalog, synchronous slice jobs.",
    version="mock",
)


def _minimal_3mf() -> bytes:
    ct = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0"'
        ' Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        "</Relationships>"
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources/><build/></model>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model)
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps({"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": 256}),
        )
    return buf.getvalue()


_MOCK_3MF = _minimal_3mf()


def _new_job() -> str:
    jid = str(uuid.uuid4())
    _jobs[jid] = "completed"
    return jid


@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "orcaslicer_installed": True,
        "orcaslicer_version": "mock-2.4.2",
        "config_mounted": True,
        "system_profiles_available": True,
        "catalog_loaded": True,
        "catalog_building": False,
        "catalog_profile_count": {"machine": 1, "process": 1, "filament": 1},
        "active_jobs": len(_jobs),
    }


@app.get("/api/profiles")
async def get_profiles(
    manufacturer: Optional[str] = None,
    model: Optional[str] = None,
    nozzle: Optional[str] = None,
    refresh: bool = False,
):
    return {"machine": [_MACHINE], "process": [_PROCESS], "filament": [_FILAMENT]}


@app.post("/api/profiles/merged-config")
async def merged_config(body: dict):
    return JSONResponse(_MERGED_CONFIG)


@app.get("/api/profiles/{profile_uuid}")
async def get_profile(profile_uuid: str):
    entry = _CATALOG.get(profile_uuid)
    if entry is None:
        raise HTTPException(404, f"Profile '{profile_uuid}' not found")
    return entry


@app.post("/api/slice/start")
async def slice_start(
    file: UploadFile = File(...),
    plate: int = Form(...),
    machine_uuid: Optional[str] = Form(None),
    process_uuid: str = Form(...),
    filament_uuids: str = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: bool = Form(True),
    extra_config: Optional[str] = Form(None),
    manufacturer: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    nozzle: Optional[str] = Form(None),
):
    return {"job_id": _new_job(), "status": "pending", "message": "Mock slice started."}


@app.post("/api/slice/prepared")
async def slice_prepared(
    file: UploadFile = File(...),
    plate: int = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: bool = Form(True),
):
    return {"job_id": _new_job(), "status": "pending", "message": "Mock slice started."}


@app.get("/api/slice/status/{job_id}")
async def slice_status(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    return {
        "job_id": job_id,
        "status": _jobs[job_id],
        "output_format": "gcode",
        "sliced_file": f"{job_id}.gcode",
        "error": None,
    }


@app.get("/api/slice/download/{job_id}")
async def slice_download(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    if _jobs[job_id] != "completed":
        raise HTTPException(400, f"Job not complete (status={_jobs[job_id]})")
    del _jobs[job_id]
    return Response(content=_MOCK_GCODE, media_type="application/octet-stream")


@app.post("/api/pack")
async def pack_stls(request: Request):
    return Response(content=_MOCK_3MF, media_type="application/octet-stream")


@app.post("/api/arrange")
async def arrange(request: Request):
    return Response(content=_MOCK_3MF, media_type="application/octet-stream")


@app.get("/api/test/known-profile", include_in_schema=False)
async def test_known_profile():
    """Stable profile UUIDs — use in tests to seed printer/job fixtures without
    hardcoding UUIDs that may drift. Hidden from the public OpenAPI schema."""
    return {
        "machine_uuid": MACHINE_UUID,
        "machine_name": _MACHINE["name"],
        "process_uuid": PROCESS_UUID,
        "process_name": _PROCESS["name"],
        "filament_uuid": FILAMENT_UUID,
        "filament_name": _FILAMENT["name"],
    }
