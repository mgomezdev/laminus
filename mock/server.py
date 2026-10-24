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
from fastapi.responses import JSONResponse, Response

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


def _new_job(filename: str) -> str:
    jid = str(uuid.uuid4())
    _v1_jobs[jid] = {"filename": filename, "created_at": time.time()}
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
        "active_jobs": 0,
    }




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
    return {"job_id": _new_job(file.filename), "status": "pending", "message": "Mock slice started."}






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


# ---- /api/v1 --------------------------------------------------------------------
# Mirrors app/api_v1.py: same versioned shapes and error envelope, canned data.
import base64  # noqa: E402
import hashlib  # noqa: E402
import time  # noqa: E402

from fastapi import Query  # noqa: E402
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

V1 = "/api/v1"
_MAX_PAGE = 200
_v1_jobs: dict[str, dict] = {}      # job_id -> {"filename", "created_at"}
_v1_downloaded: set[str] = set()
_idem: dict[str, dict] = {}         # Idempotency-Key -> {"fp", "job_id"}

_DEFAULT_CODES = {
    400: "bad_request", 404: "not_found", 405: "method_not_allowed", 409: "conflict",
    422: "unprocessable", 500: "internal_error", 503: "unavailable",
}


def _is_v1(request: Request) -> bool:
    return request.url.path == V1 or request.url.path.startswith(V1 + "/")


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _envelope(request: Request, status: int, detail) -> JSONResponse:
    details = None
    if isinstance(detail, dict) and "code" in detail:
        code, message = detail["code"], detail.get("message", "")
    elif isinstance(detail, str):
        code, message = _DEFAULT_CODES.get(status, "error"), detail
    else:
        code = "validation_error" if status == 422 else _DEFAULT_CODES.get(status, "error")
        message, details = ("Request validation failed." if status == 422 else "Request failed."), detail
    body = {"error": {"code": code, "message": message, "status": status}, "request_id": _rid(request)}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


@app.middleware("http")
async def _v1_headers(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    if _is_v1(request):
        response.headers["API-Version"] = "v1"
    return response


@app.exception_handler(StarletteHTTPException)
async def _v1_http_exc(request: Request, exc: StarletteHTTPException):
    if _is_v1(request):
        return _envelope(request, exc.status_code, exc.detail)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _v1_validation_exc(request: Request, exc: RequestValidationError):
    if _is_v1(request):
        return _envelope(request, 422, json.loads(json.dumps(exc.errors(), default=str)))
    return await request_validation_exception_handler(request, exc)


def _encode_cursor(key: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> list:
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(key, list):
            raise ValueError
        return key
    except (ValueError, TypeError):
        raise _err(422, "invalid_cursor", "cursor is malformed or from a different listing.")


def _paginate(records: list[dict], key_fn, limit: int, cursor: Optional[str], reverse: bool = False):
    ordered = sorted(records, key=key_fn, reverse=reverse)
    if cursor:
        after = _decode_cursor(cursor)
        ordered = [r for r in ordered if (key_fn(r) < after if reverse else key_fn(r) > after)]
    page = ordered[:limit]
    nxt = _encode_cursor(key_fn(page[-1])) if len(ordered) > limit else None
    return page, {"limit": limit, "next_cursor": nxt}


@app.get(V1, tags=["v1"], summary="API capability discovery")
async def v1_capabilities():
    return {
        "api_version": "v1",
        "supported_versions": ["v1"],
        "legacy": {"prefix": "/api", "deprecated": True, "retired": True, "sunset": "2026-10-24", "policy": "docs/api-versioning.md"},
        "engine": {"name": "orcaslicer", "version": "2.4.2", "binary_version": "mock-2.4.2"},
        "ready": {"catalog": True},
        "features": ["jobs", "jobs.idempotency", "pagination.cursor", "profiles", "pack", "arrange", "health",
                     "slice.prepared", "profiles.rescan", "profiles.broken", "profiles.merged_config",
                     "profiles.neutral"],
        "limits": {"max_concurrent_jobs": 1, "job_history_limit": 200, "max_page_size": _MAX_PAGE},
    }


def _job_links(job_id: str) -> dict:
    return {"self": f"{V1}/jobs/{job_id}", "events": f"{V1}/jobs/{job_id}/events",
            "artifact": f"{V1}/jobs/{job_id}/artifact"}


def _job_view(job_id: str) -> Optional[dict]:
    rec = _v1_jobs.get(job_id)
    if rec is None:
        return None
    return {
        "id": job_id, "status": "completed", "filename": rec["filename"], "output_format": "gcode",
        "error": None,
        "artifact": {"name": f"{job_id}.gcode", "available": job_id not in _v1_downloaded},
        "created_at": rec["created_at"], "updated_at": rec["created_at"],
        "links": _job_links(job_id),
    }


@app.post(f"{V1}/jobs", tags=["v1 jobs"], status_code=202, summary="Create a slice job")
async def v1_create_job(
    request: Request,
    file: UploadFile = File(...),
    plate: int = Form(...),
    machine_uuid: Optional[str] = Form(None),
    process_uuid: str = Form(...),
    filament_uuids: str = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: Optional[str] = Form(None),
    extra_config: Optional[str] = Form(None),
    manufacturer: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    nozzle: Optional[str] = Form(None),
):
    key = request.headers.get("Idempotency-Key")
    fp = None
    if key is not None:
        if not key.strip() or len(key) > 255:
            raise _err(422, "invalid_idempotency_key", "Idempotency-Key must be 1-255 characters.")
        fp = hashlib.sha256(json.dumps(
            [hashlib.sha256(await file.read()).hexdigest(), plate, machine_uuid, process_uuid, filament_uuids,
             export_3mf, geometry_only_retry, extra_config, manufacturer, model, nozzle],
            sort_keys=True).encode()).hexdigest()
        await file.seek(0)
        prior = _idem.get(key)
        if prior is not None:
            if prior["fp"] != fp:
                raise _err(422, "idempotency_key_reuse", "Idempotency-Key was already used with a different request.")
            view = _job_view(prior["job_id"])
            return JSONResponse(view, status_code=202,
                                headers={"Idempotent-Replay": "true", "Location": view["links"]["self"]})
    if plate < 1:
        raise HTTPException(422, "plate must be >= 1.")
    _check_export_3mf(export_3mf)
    job_id = str(uuid.uuid4())
    _v1_jobs[job_id] = {"filename": file.filename, "created_at": time.time()}
    if key is not None:
        _idem[key] = {"fp": fp, "job_id": job_id}
    view = _job_view(job_id)
    return JSONResponse(view, status_code=202, headers={"Location": view["links"]["self"]})


@app.get(f"{V1}/jobs", tags=["v1 jobs"], summary="List jobs (newest first, cursor-paginated)")
async def v1_list_jobs(limit: int = Query(50, ge=1, le=_MAX_PAGE), cursor: Optional[str] = None,
                       status: Optional[str] = None):
    records = [{"id": j, **r} for j, r in _v1_jobs.items() if not status or status == "completed"]
    page, meta = _paginate(records, lambda r: [r["created_at"], r["id"]], limit, cursor, reverse=True)
    return {"data": [_job_view(r["id"]) for r in page], "page": meta}


@app.get(f"{V1}/jobs/{{job_id}}", tags=["v1 jobs"], summary="Get a job")
async def v1_get_job(job_id: str):
    view = _job_view(job_id)
    if view is None:
        raise _err(404, "job_not_found", "Job not found.")
    return view


@app.get(f"{V1}/jobs/{{job_id}}/artifact", tags=["v1 jobs"], summary="Download the sliced artifact")
async def v1_job_artifact(job_id: str):
    if job_id not in _v1_jobs:
        raise _err(404, "job_not_found", "Job not found.")
    if job_id in _v1_downloaded:
        raise _err(404, "artifact_gone", "Artifact was already downloaded, expired or lost in a restart.")
    _v1_downloaded.add(job_id)
    return Response(content=_MOCK_GCODE, media_type="application/octet-stream")


@app.get(f"{V1}/profiles", tags=["v1 profiles"], summary="List profiles (cursor-paginated)")
async def v1_list_profiles(type: Optional[str] = Query(None, pattern="^(machine|process|filament)$"),
                           manufacturer: Optional[str] = None, model: Optional[str] = None,
                           nozzle: Optional[str] = None, q: Optional[str] = None,
                           limit: int = Query(50, ge=1, le=_MAX_PAGE), cursor: Optional[str] = None):
    given = [p for p in (manufacturer, model, nozzle) if p is not None]
    if given and len(given) != 3:
        raise _err(422, "partial_machine_filter",
                   "Provide all three of manufacturer, model, and nozzle together, or none.")
    records = [e for e in _CATALOG.values() if not type or e["type"] == type]
    if q:
        records = [r for r in records if q.lower() in r["name"].lower()]
    page, meta = _paginate(records, lambda r: [r["name"], r["uuid"]], limit, cursor)
    return {"data": page, "page": meta}


@app.get(f"{V1}/profiles/{{profile_uuid}}", tags=["v1 profiles"], summary="Get one profile")
async def v1_get_profile(profile_uuid: str):
    entry = _CATALOG.get(profile_uuid)
    if entry is None:
        raise _err(404, "profile_not_found", f"Profile UUID '{profile_uuid}' not found.")
    return entry
