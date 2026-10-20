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
import re
import uuid
import zipfile
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

# Stable UUIDs — guaranteed constant across releases so tests can hardcode them
MACHINE_UUID = "00000000-mock-mach-0004-elegoocc04noz"
PROCESS_UUID = "00000000-mock-proc-016m-optimal00000"
FILAMENT_UUID = "00000000-mock-fila-0000-elegooplaefcc"

_MACHINE = {
    "uuid": MACHINE_UUID,
    "name": "Elegoo Centauri Carbon 0.4 nozzle",
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
    "name": "0.16mm Optimal @Elegoo CC 0.4 nozzle",
    "layer_height": 0.16,
    "compatible_printers": ["Elegoo Centauri Carbon 0.4 nozzle"],
    "source": "mock",
    "rel_path": "mock/process.json",
    "type": "process",
}
_FILAMENT = {
    "uuid": FILAMENT_UUID,
    "name": "Elegoo PLA @ECC",
    "display_name": "Elegoo PLA",
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
    "compatible_printers": ["Elegoo Centauri Carbon 0.4 nozzle"],
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


@app.exception_handler(StarletteHTTPException)
async def _http_exc(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        {"error": {"code": "error", "message": str(exc.detail), "status": exc.status_code}, "request_id": "mock"},
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    return JSONResponse(
        {"error": {"code": "validation_error", "message": "Request validation failed.", "status": 422,
                   "details": json.loads(json.dumps(exc.errors(), default=str))}, "request_id": "mock"},
        status_code=422,
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


def _check_export_3mf(export_3mf: Optional[str]) -> None:
    """Mirror the real app's export_3mf guard.

    The real service passes this straight to OrcaSlicer as --export-3mf and joins
    it onto the output dir, so it rejects any path component (an absolute path
    replaces the join base outright) and requires a .3mf suffix. The mock has to
    reject the same inputs, or a test can pass here and fail against the real one.
    """
    if not export_3mf:
        return
    if export_3mf in (".", "..") or "/" in export_3mf or "\\" in export_3mf:
        raise HTTPException(422, "export_3mf must not contain path separators")
    if not export_3mf.lower().endswith(".3mf"):
        raise HTTPException(422, "export_3mf must end with .3mf")


@app.get("/api/v1/health")
async def health():
    return {
        "status": "healthy",
        "orcaslicer_installed": True,
        "orcaslicer_version": "mock-2.4.2",
        "orca_version": "2.4.2",
        "config_mounted": True,
        "system_profiles_available": True,
        "catalog_loaded": True,
        "catalog_building": False,
        "catalog_profile_count": {"machine": 1, "process": 1, "filament": 1},
        "catalog_skipped_count": 0,
        "active_jobs": len(_jobs),
    }


@app.get("/api/v1/profiles")
async def get_profiles(
    type: Optional[str] = None,
    manufacturer: Optional[str] = None,
    model: Optional[str] = None,
    nozzle: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
):
    records = [e for e in (_MACHINE, _PROCESS, _FILAMENT) if not type or e["type"] == type]
    if q:
        records = [e for e in records if q.lower() in e["name"].lower()]
    return {"data": records, "page": {"limit": limit, "next_cursor": None}}


@app.post("/api/v1/profiles/merged-config")
async def merged_config(body: dict):
    return JSONResponse(_MERGED_CONFIG)


@app.post("/api/v1/profiles/rescan")
async def rescan_profiles():
    return {"status": "started", "message": "Catalog rescan started. Poll /api/v1/health until catalog_building is false."}


@app.get("/api/v1/profiles/broken")
async def get_broken_profiles():
    return {"count": 0, "broken": []}


# ---- neutral (model / nozzle-variant) API -------------------------------------
# Mirrors app/neutral_profiles.py for the mock's single machine. Constant version.

CATALOG_VERSION = "mock-catalog-v1"


def _ref(kind: str, name: str) -> str:
    return f"{kind}:{re.sub(r'[^a-z0-9.]+', '-', name.lower()).strip('-')}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


MACHINE_REF = _ref("machine", _MACHINE["name"])


def _query(body: dict) -> dict:
    for k in ("manufacturer", "model"):
        if not isinstance(body.get(k), str):
            raise HTTPException(422, f"'{k}' is required.")
    return body


def _neutral(body: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        {"catalog_version": CATALOG_VERSION, **body},
        status_code=status_code,
        headers={"X-Catalog-Version": CATALOG_VERSION},
    )


def _machine_json() -> dict:
    return {
        "ref": MACHINE_REF, "uuid": MACHINE_UUID, "name": _MACHINE["name"],
        "nozzle_diameter_mm": _MACHINE["nozzle_diameter"], "flow": "standard",
    }


def _match(body: dict) -> Optional[str]:
    q = _query(body)
    mfr = _norm(_MACHINE["manufacturer"])
    if _norm(q["manufacturer"]) not in mfr and mfr not in _norm(q["manufacturer"]):
        return None
    keys = {_norm(q["model"]), _norm(q.get("model_key") or "")}
    return "exact_model" if _norm(_MACHINE["model"]) in keys else "vendor_only"


@app.post("/api/v1/profiles/machines/suggest")
async def suggest_machines(body: dict):
    reason = _match(body)
    cands = [] if reason is None else [
        {**_machine_json(), "confidence": 1.0 if reason == "exact_model" else 0.1, "match_reason": reason}
    ]
    return _neutral({"candidates": cands})


@app.post("/api/v1/profiles/models/variants")
async def model_variants(body: dict):
    variants = [{**_machine_json(), "is_default_guess": True}] if _match(body) == "exact_model" else []
    return _neutral({"variants": variants})


@app.get("/api/v1/profiles/machines/{machine_ref}/presets")
async def machine_presets(machine_ref: str, kind: Optional[str] = None):
    if kind not in (None, "process", "filament"):
        return _neutral({"error": "invalid_kind"}, 422)
    if machine_ref != MACHINE_REF:
        return _neutral({"error": "unknown_machine_ref"}, 404)
    model = f"{_MACHINE['manufacturer']} {_MACHINE['model']}"
    presets = [
        {"ref": _ref(e["type"], e["name"]), "uuid": e["uuid"], "name": e["name"], "kind": e["type"],
         "compatible_models": [model]}
        for e in (_PROCESS, _FILAMENT) if kind in (None, e["type"])
    ]
    return _neutral({"machine_ref": machine_ref, "presets": presets})


@app.get("/api/v1/profiles/machines/{machine_ref}/supports")
async def machine_supports(machine_ref: str, bed_x: Optional[float] = None, bed_y: Optional[float] = None):
    reason = None
    if machine_ref != MACHINE_REF:
        reason = "unknown_machine_ref"
    elif any(w is not None and abs(w - h) > 1.0 for w, h in ((bed_x, _MACHINE["bed_size_x"]), (bed_y, _MACHINE["bed_size_y"]))):
        reason = "bed_mismatch"
    return _neutral({"machine_ref": machine_ref, "supported": reason is None, "reason": reason})


@app.get("/api/v1/profiles/{profile_uuid}")
async def get_profile(profile_uuid: str):
    entry = _CATALOG.get(profile_uuid)
    if entry is None:
        raise HTTPException(404, f"Profile '{profile_uuid}' not found")
    return entry


def _job_view(job_id: str) -> dict:
    done = _jobs[job_id] == "completed"
    return {
        "id": job_id,
        "status": _jobs[job_id],
        "filename": "model.stl",
        "output_format": "gcode",
        "error": None,
        "artifact": {"name": f"{job_id}.gcode", "available": done},
        "created_at": 0,
        "updated_at": 0,
        "links": {
            "self": f"/api/v1/jobs/{job_id}",
            "events": f"/api/v1/jobs/{job_id}/events",
            "artifact": f"/api/v1/jobs/{job_id}/artifact",
        },
    }


@app.post("/api/v1/jobs", status_code=202)
async def create_job(
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
    if plate < 1:
        raise HTTPException(422, "plate must be >= 1.")
    _check_export_3mf(export_3mf)
    jid = _new_job()
    return JSONResponse(_job_view(jid), status_code=202, headers={"Location": f"/api/v1/jobs/{jid}"})


@app.post("/api/v1/slice/prepared")
async def slice_prepared(
    file: UploadFile = File(...),
    plate: int = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: bool = Form(True),
):
    if plate < 1:
        raise HTTPException(422, "plate must be >= 1.")
    _check_export_3mf(export_3mf)
    return {"job_id": _new_job(), "status": "pending", "message": "Mock slice started."}


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    return _job_view(job_id)


@app.get("/api/v1/jobs/{job_id}/artifact")
async def job_artifact(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    if _jobs[job_id] != "completed":
        raise HTTPException(409, f"Job not complete (status={_jobs[job_id]})")
    del _jobs[job_id]
    return Response(content=_MOCK_GCODE, media_type="application/octet-stream")


@app.post("/api/v1/pack")
async def pack_stls(request: Request):
    return Response(content=_MOCK_3MF, media_type="application/octet-stream")


@app.post("/api/v1/arrange")
async def arrange(request: Request):
    return Response(content=_MOCK_3MF, media_type="application/octet-stream")


@app.get("/api/v1/test/known-profile", include_in_schema=False)
async def test_known_profile(machine_name: str | None = None):
    """Stable profile UUIDs — use in tests to seed printer/job fixtures without
    hardcoding UUIDs that may drift. Hidden from the public OpenAPI schema.

    The mock only ever knows about one machine, so `machine_name` (if given)
    must match it exactly — same 404-on-mismatch contract as the real app."""
    if machine_name is not None and machine_name != _MACHINE["name"]:
        raise HTTPException(status_code=404, detail=f"Machine {machine_name!r} not in catalog.")
    return {
        "machine_uuid": MACHINE_UUID,
        "machine_name": _MACHINE["name"],
        "process_uuid": PROCESS_UUID,
        "process_name": _PROCESS["name"],
        "filament_uuid": FILAMENT_UUID,
        "filament_name": _FILAMENT["name"],
    }
