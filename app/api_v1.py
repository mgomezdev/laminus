"""Laminus API v1: stable, resource-oriented routes under ``/api/v1``.

Contract (see docs/api-versioning.md):
  * every v1 error uses one envelope: ``{"error": {"code", "message", "status", "details"?},
    "request_id"}``
  * list endpoints use opaque keyset cursors: ``{"data": [...], "page": {"limit", "next_cursor"}}``
  * ``POST /api/v1/jobs`` honours ``Idempotency-Key``
  * ``GET /api/v1`` is the capability-discovery document
  * there are no versionless routes: every endpoint is under ``/api/v1``

The module is installed by ``main.py`` (``install(app, main_module)``) so handlers read the
live job table / catalog through ``m`` at call time.
"""
import base64
import hashlib
import json
import os
import time
import uuid
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

API_VERSION = "v1"
PREFIX = "/api/v1"
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
_MAX_PAGE = 200

_DEFAULT_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 408: "timeout", 409: "conflict", 413: "payload_too_large",
    422: "unprocessable", 429: "rate_limited", 500: "internal_error", 503: "unavailable",
}


def _err(status: int, code: str, message: str, headers: Optional[dict] = None):
    return HTTPException(status_code=status, detail={"code": code, "message": message}, headers=headers)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _envelope(request: Request, status: int, detail, headers: Optional[dict] = None) -> JSONResponse:
    details = None
    if isinstance(detail, dict) and "code" in detail:
        code, message = detail["code"], detail.get("message", "")
    elif isinstance(detail, str):
        code, message = _DEFAULT_CODES.get(status, "error"), detail
    else:  # validation error list or arbitrary structure
        code, message, details = ("validation_error" if status == 422 else _DEFAULT_CODES.get(status, "error"),
                                  "Request validation failed." if status == 422 else "Request failed.", detail)
    body = {"error": {"code": code, "message": message, "status": status}, "request_id": _request_id(request)}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body, headers=headers)


def _is_api(request: Request) -> bool:
    """Everything under /api is v1; unknown /api/* paths get the envelope too."""
    return request.url.path == "/api" or request.url.path.startswith("/api/")


# ------------------------------------------------------------------ pagination

def _encode_cursor(key: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> list:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        key = json.loads(raw)
        if not isinstance(key, list):
            raise ValueError
        return key
    except (ValueError, TypeError):
        raise _err(422, "invalid_cursor", "cursor is malformed or from a different listing.")


def paginate(records: list[dict], key_fn, limit: int, cursor: Optional[str], reverse: bool):
    """Keyset page over ``records``; ``key_fn(record)`` must be a unique, totally ordered list."""
    ordered = sorted(records, key=key_fn, reverse=reverse)
    if cursor:
        after = _decode_cursor(cursor)
        ordered = [r for r in ordered if (key_fn(r) < after if reverse else key_fn(r) > after)]
    page = ordered[:limit]
    nxt = _encode_cursor(key_fn(page[-1])) if len(ordered) > limit else None
    return page, {"limit": limit, "next_cursor": nxt}


# ------------------------------------------------------------------ install

def install(app: FastAPI, m) -> None:
    """Register v1 routes, error envelope and request ids. Every API route lives under /api/v1."""
    idem: dict[str, dict] = {}

    # ---- cross-cutting: request id, version header
    @app.middleware("http")
    async def _headers(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if _is_api(request):
            response.headers["API-Version"] = API_VERSION
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        if _is_api(request):
            return _envelope(request, exc.status_code, exc.detail, getattr(exc, "headers", None))
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        if _is_api(request):
            return _envelope(request, 422, json.loads(json.dumps(exc.errors(), default=str)))
        return await request_validation_exception_handler(request, exc)

    def _catalog():
        if m.catalog is None:
            raise _err(503, "catalog_not_ready", "Profile catalog not yet ready. Retry shortly.",
                       headers={"Retry-After": "5"})
        return m.catalog

    # ---- discovery
    @app.get(PREFIX, tags=["v1"], summary="API capability discovery",
             description="Version, engine, readiness, feature flags and limits. Clients should "
                         "feature-detect from `features` rather than parsing versions.")
    async def capabilities():
        return {
            "api_version": API_VERSION,
            "supported_versions": [API_VERSION],
            "engine": {"name": "orcaslicer", "version": m._orca_version(),
                       "binary_version": m._orcaslicer_version},
            "ready": {"catalog": m.catalog is not None and m.catalog.is_built},
            "features": ["jobs", "jobs.idempotency", "jobs.events", "pagination.cursor",
                         "profiles", "projects.inspect", "projects.subset", "projects.merge",
                         "projects.previews", "pack", "arrange", "slice.prepared",
                         "slice.thumbnail"],
            "limits": {"max_concurrent_jobs": m.MAX_CONCURRENT_JOBS,
                       "job_history_limit": m.JOB_HISTORY_LIMIT,
                       "max_page_size": _MAX_PAGE},
        }

    # ---- jobs
    def _links(job_id: str) -> dict:
        return {"self": f"{PREFIX}/jobs/{job_id}", "events": f"{PREFIX}/jobs/{job_id}/events",
                "artifact": f"{PREFIX}/jobs/{job_id}/artifact"}

    def _view(job_id: str) -> Optional[dict]:
        live, hist = m.jobs.get(job_id), m.job_history.get(job_id)
        if live is None and hist is None:
            return None
        src = {**(hist or {})}
        if live is not None:
            src["status"] = live["status"]
            src["error"] = live.get("error")
            src["sliced_file"] = live.get("sliced_file") or src.get("sliced_file")
            src["output_format"] = live.get("output_format", src.get("output_format"))
        sliced = src.get("sliced_file")
        available = bool(live is not None and not live.get("_stub") and src.get("status") == "completed"
                         and sliced and os.path.exists(sliced))
        return {
            "id": job_id, "status": src.get("status"), "filename": src.get("filename"),
            "output_format": src.get("output_format"), "error": src.get("error"),
            "artifact": {"name": os.path.basename(sliced) if sliced else None, "available": available},
            "created_at": src.get("created_at"), "updated_at": src.get("updated_at"),
            "links": _links(job_id),
        }

    @app.post(f"{PREFIX}/jobs", tags=["v1 jobs"], status_code=202, summary="Create a slice job",
              description="Upload a model plus profile UUIDs to start a slice. Send an "
                          "`Idempotency-Key` header to make retries safe: the same key with the "
                          "same body returns the original job (`Idempotent-Replay: true`); the same "
                          "key with a different body is a 422 `idempotency_key_reuse`; a key whose "
                          "first request is still being processed is a 409. Keys are remembered "
                          "for 24 h, in memory (not across restarts).",
              responses={409: {"description": "Idempotency key in use by an in-flight request"},
                         422: {"description": "Validation error or idempotency key reuse"},
                         503: {"description": "Catalog not ready or too many active jobs"}})
    async def create_job(
        request: Request, background_tasks: BackgroundTasks,
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
        extra_config: Optional[str] = Form(None, description="JSON object merged into project settings"),
    ):
        key = request.headers.get("Idempotency-Key")
        fp = None
        if key is not None:
            if not key.strip() or len(key) > 255:
                raise _err(422, "invalid_idempotency_key", "Idempotency-Key must be 1-255 characters.")
            now = time.time()
            for k in [k for k, v in idem.items() if now - v["ts"] > IDEMPOTENCY_TTL_SECONDS]:
                del idem[k]
            digest = hashlib.sha256()
            while chunk := await file.read(1 << 20):
                digest.update(chunk)
            await file.seek(0)
            fp = hashlib.sha256(json.dumps(
                [digest.hexdigest(), machine_uuid, manufacturer, model, nozzle, process_uuid,
                 filament_uuids, plate, export_3mf, geometry_only_retry, extra_config],
                sort_keys=True).encode()).hexdigest()
            prior = idem.get(key)
            if prior is not None:
                if prior["fp"] != fp:
                    raise _err(422, "idempotency_key_reuse",
                               "Idempotency-Key was already used with a different request.")
                if prior["job_id"] is None:
                    raise _err(409, "idempotency_in_flight", "A request with this key is still being processed.",
                               headers={"Retry-After": "1"})
                view = _view(prior["job_id"]) or {"id": prior["job_id"], "status": "unknown",
                                                  "links": _links(prior["job_id"])}
                return JSONResponse(status_code=202, content=view, headers={
                    "Idempotent-Replay": "true", "Location": view["links"]["self"]})
            idem[key] = {"fp": fp, "job_id": None, "ts": now}
        try:
            result = await m.start_slice(
                background_tasks=background_tasks, file=file, machine_uuid=machine_uuid,
                manufacturer=manufacturer, model=model, nozzle=nozzle, process_uuid=process_uuid,
                filament_uuids=filament_uuids, plate=plate, export_3mf=export_3mf,
                geometry_only_retry=geometry_only_retry, extra_config=extra_config)
        except BaseException:
            if key is not None:
                idem.pop(key, None)  # a failed create must stay retryable under the same key
            raise
        job_id = result["job_id"]
        if key is not None:
            idem[key]["job_id"] = job_id
        view = _view(job_id)
        return JSONResponse(status_code=202, content=view, headers={"Location": view["links"]["self"]})

    @app.get(f"{PREFIX}/jobs", tags=["v1 jobs"], summary="List jobs (newest first, cursor-paginated)")
    async def list_jobs(limit: int = Query(50, ge=1, le=_MAX_PAGE), cursor: Optional[str] = None,
                        status: Optional[str] = None):
        records = [r for r in m.job_history.values() if not status or r.get("status") == status]
        page, meta = paginate(records, lambda r: [r.get("created_at") or 0, r.get("id") or ""],
                              limit, cursor, reverse=True)
        return {"data": [_view(r["id"]) for r in page], "page": meta}

    @app.get(f"{PREFIX}/jobs/{{job_id}}", tags=["v1 jobs"], summary="Get a job",
             responses={404: {"description": "Unknown job"}})
    async def get_job(job_id: str):
        view = _view(job_id)
        if view is None:
            raise _err(404, "job_not_found", "Job not found.")
        return view

    @app.get(f"{PREFIX}/jobs/{{job_id}}/events", tags=["v1 jobs"], response_class=StreamingResponse,
             summary="Stream job events (SSE)",
             description="Typed server-sent events with sequential `id`s: `log` ({message}), "
                         "`dropped` ({message}), and one terminal `completed` or `failed` "
                         "({reason}). Send `Last-Event-ID` to skip events already seen (the "
                         "buffer is replayed from the start on every connect).",
             responses={404: {"description": "Unknown job, or job data lost in a restart"}})
    async def job_events(job_id: str, request: Request):
        if job_id not in m.jobs:
            raise _err(404, "job_not_found", "Job not found.")
        job = m.jobs[job_id]
        if job.get("logger") is None:
            raise _err(404, "job_data_lost", "Job data was lost when Laminus restarted.")
        try:
            skip = int(request.headers.get("Last-Event-ID", "0"))
        except ValueError:
            skip = 0

        async def stream():
            n = 0
            async for raw in job["logger"].get_stream():
                msg = raw[len("data: "):].rstrip("\n")
                if msg == "__COMPLETED__":
                    ev, payload = "completed", {}
                elif msg.startswith("__FAILED__"):
                    ev, payload = "failed", {"reason": msg.partition(":")[2].strip()}
                elif msg.startswith("__DROPPED__"):
                    ev, payload = "dropped", {"message": msg.partition(":")[2].strip()}
                else:
                    ev, payload = "log", {"message": msg}
                n += 1
                if n > skip:
                    yield f"id: {n}\nevent: {ev}\ndata: {json.dumps(payload)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get(f"{PREFIX}/jobs/{{job_id}}/artifact", tags=["v1 jobs"], summary="Download the sliced artifact",
             description="One-time download: the job's files are deleted afterwards (its record stays "
                         "listed with `artifact.available: false`).",
             responses={404: {"description": "Unknown job or artifact gone"},
                        409: {"description": "Job not complete"}})
    async def job_artifact(job_id: str, background_tasks: BackgroundTasks):
        view = _view(job_id)
        if view is None:
            raise _err(404, "job_not_found", "Job not found.")
        if view["status"] in ("pending", "slicing"):
            raise _err(409, "job_not_complete", f"Job is {view['status']}.")
        if view["status"] == "failed":
            raise _err(409, "job_failed", view.get("error") or "Job failed.")
        if not view["artifact"]["available"]:
            raise _err(404, "artifact_gone", "Artifact was already downloaded, expired or lost in a restart.")
        return await m.download_sliced_file(job_id, background_tasks)

    # ---- profiles
    @app.get(f"{PREFIX}/profiles", tags=["v1 profiles"], summary="List profiles (cursor-paginated)",
             description="Flat list across machine/process/filament presets. `manufacturer`, `model` "
                         "and `nozzle` must be given together (compatibility filter).",
             responses={503: {"description": "Catalog not ready (see Retry-After)"}})
    async def list_profiles(type: Optional[str] = Query(None, pattern="^(machine|process|filament)$"),
                            manufacturer: Optional[str] = None, model: Optional[str] = None,
                            nozzle: Optional[str] = None, q: Optional[str] = None,
                            limit: int = Query(50, ge=1, le=_MAX_PAGE), cursor: Optional[str] = None):
        cat = _catalog()
        given = [p for p in (manufacturer, model, nozzle) if p is not None]
        if given and len(given) != 3:
            raise _err(422, "partial_machine_filter",
                       "Provide all three of manufacturer, model, and nozzle together, or none.")
        grouped = cat.as_dict(manufacturer=manufacturer, model=model, nozzle=nozzle)
        records = [{**e, "type": e.get("type", kind)} for kind, es in grouped.items()
                   if not type or kind == type for e in es]
        if q:
            records = [r for r in records if q.lower() in (r.get("name") or "").lower()]
        page, meta = paginate(records, lambda r: [r.get("name") or "", r["uuid"]], limit, cursor, reverse=False)
        return {"data": page, "page": meta}

    @app.get(f"{PREFIX}/profiles/{{profile_uuid}}", tags=["v1 profiles"], summary="Get one profile",
             responses={404: {"description": "Unknown profile"}, 503: {"description": "Catalog not ready"}})
    async def get_profile(profile_uuid: str):
        cat = _catalog()
        entry = cat.get_by_uuid(profile_uuid)
        if entry is None:
            raise _err(404, "profile_not_found", f"Profile UUID '{profile_uuid}' not found.")
        return cat._public(entry)
