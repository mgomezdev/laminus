"""Laminus API v1: stable, resource-oriented routes under ``/api/v1``.

Contract (see docs/api-versioning.md):
  * every v1 error uses one envelope: ``{"error": {"code", "message", "status", "details"?},
    "request_id"}``
  * list endpoints use opaque keyset cursors: ``{"data": [...], "page": {"limit", "next_cursor"}}``
  * ``POST /api/v1/jobs`` honours ``Idempotency-Key``
  * ``GET /api/v1`` is the capability-discovery document
  * legacy ``/api/*`` routes keep working unchanged; once ``LEGACY_DEPRECATION_HEADERS`` is
    enabled (the default), those with a v1 equivalent carry ``Deprecation`` / ``Link: <v1 route>;
    rel="successor-version"`` response headers and the agreed Sunset date

The module is installed by ``main.py`` (``install(app, main_module)``) so handlers read the
live job table / catalog through ``m`` at call time.
"""
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)
LEGACY_SUNSET = "Sat, 24 Apr 2027 00:00:00 GMT"

API_VERSION = "v1"
PREFIX = "/api/v1"
IDEMPOTENCY_TTL_SECONDS = 24 * 3600
_MAX_PAGE = 200

# legacy route template -> v1 route template (drives Deprecation / successor-version headers)
LEGACY_TO_V1 = {
    "/api/slice/start": f"{PREFIX}/jobs",
    "/api/slice/status/{job_id}": f"{PREFIX}/jobs/{{job_id}}",
    "/api/slice/logs/{job_id}": f"{PREFIX}/jobs/{{job_id}}/events",
    "/api/slice/download/{job_id}": f"{PREFIX}/jobs/{{job_id}}/artifact",
    "/api/jobs": f"{PREFIX}/jobs",
    "/api/profiles": f"{PREFIX}/profiles",
    "/api/profiles/{profile_uuid}": f"{PREFIX}/profiles/{{profile_uuid}}",
    "/api/3mf/objects": f"{PREFIX}/projects/inspect",
    "/api/arrange/subset": f"{PREFIX}/projects/subset",
    "/api/arrange/merge": f"{PREFIX}/projects/merge",
}

# (legacy path, method, v1 path, tag, summary): v1 serves the legacy handler unchanged apart from
# the error envelope and response headers.
V1_ALIASES = [
    ("/api/3mf/objects", "POST", f"{PREFIX}/projects/inspect", "v1 projects", "List objects in a 3MF project"),
    ("/api/arrange/subset", "POST", f"{PREFIX}/projects/subset", "v1 projects",
     "Subset a 3MF by object x quantity and arrange"),
    ("/api/arrange/merge", "POST", f"{PREFIX}/projects/merge", "v1 projects",
     "Merge selections from several 3MFs and arrange"),
    ("/api/health", "GET", f"{PREFIX}/health", "v1 health", "Service health"),
    ("/api/pack", "POST", f"{PREFIX}/pack", "v1 projects", "Pack STL/3MF files onto plates"),
    ("/api/arrange", "POST", f"{PREFIX}/arrange", "v1 projects", "Arrange and orient a 3MF"),
    ("/api/3mf/previews", "POST", f"{PREFIX}/projects/previews", "v1 projects", "Render 3MF plate previews"),
    ("/api/slice/prepared", "POST", f"{PREFIX}/slice/prepared", "v1 jobs", "Slice a prepared 3MF"),
    ("/api/slice/thumbnail", "POST", f"{PREFIX}/slice/thumbnail", "v1 jobs", "Render a slice thumbnail"),
    ("/api/profiles/rescan", "POST", f"{PREFIX}/profiles/rescan", "v1 profiles", "Force a catalog rebuild"),
    ("/api/profiles/broken", "GET", f"{PREFIX}/profiles/broken", "v1 profiles",
     "Profiles dropped for unresolved inheritance"),
    ("/api/profiles/upload", "POST", f"{PREFIX}/profiles/upload", "v1 profiles", "Upload a user profile"),
    ("/api/profiles/merged-config", "POST", f"{PREFIX}/profiles/merged-config", "v1 profiles",
     "Resolve a merged project config"),
    ("/api/profiles/machines/suggest", "POST", f"{PREFIX}/profiles/machines/suggest", "v1 profiles",
     "Suggest machine presets"),
    ("/api/profiles/models/variants", "POST", f"{PREFIX}/profiles/models/variants", "v1 profiles",
     "Nozzle variants for a model"),
    ("/api/profiles/machines/{machine_ref}/presets", "GET", f"{PREFIX}/profiles/machines/{{machine_ref}}/presets",
     "v1 profiles", "Presets compatible with a machine"),
    ("/api/profiles/machines/{machine_ref}/supports", "GET", f"{PREFIX}/profiles/machines/{{machine_ref}}/supports",
     "v1 profiles", "Capabilities of a machine"),
    ("/api/cleanup", "POST", f"{PREFIX}/cleanup", "v1 maintenance", "Clean up stale job directories"),
]
LEGACY_TO_V1.update({old: new for old, _m, new, _t, _s in V1_ALIASES})

def _legacy_deprecation_enabled() -> bool:
    # Consumers migrated (BIZ-333); retain an emergency signalling opt-out.
    return os.environ.get("LEGACY_DEPRECATION_HEADERS", "true").strip().lower() in ("1", "true", "yes", "on")


_DEFAULT_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 408: "timeout", 409: "conflict", 413: "payload_too_large",
    422: "unprocessable", 429: "rate_limited", 500: "internal_error", 503: "unavailable",
}


def _err(status: int, code: str, message: str, headers: Optional[dict] = None):
    return HTTPException(status_code=status, detail={"code": code, "message": message}, headers=headers)


class DetailedHTTPException(HTTPException):
    """HTTPException with structured ``details`` shown only in the v1 envelope.

    Legacy routes keep serialising ``detail`` (a plain message) so their contract is unchanged.
    """

    def __init__(self, status_code: int, detail: str, code: str, details: list):
        super().__init__(status_code=status_code, detail=detail)
        self.code, self.details = code, details


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def _envelope(request: Request, status: int, detail, headers: Optional[dict] = None) -> JSONResponse:
    details = None
    if isinstance(detail, dict) and "code" in detail:
        code, message, details = detail["code"], detail.get("message", ""), detail.get("details")
    elif isinstance(detail, str):
        code, message = _DEFAULT_CODES.get(status, "error"), detail
    else:  # validation error list or arbitrary structure
        code, message, details = ("validation_error" if status == 422 else _DEFAULT_CODES.get(status, "error"),
                                  "Request validation failed." if status == 422 else "Request failed.", detail)
    body = {"error": {"code": code, "message": message, "status": status}, "request_id": _request_id(request)}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body, headers=headers)


def _is_v1(request: Request) -> bool:
    return request.url.path == PREFIX or request.url.path.startswith(PREFIX + "/")


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
    """Register v1 routes, error envelope, request ids and legacy deprecation headers."""
    idem: dict[str, dict] = {}

    # ---- cross-cutting: request id, version header, deprecation of legacy routes
    @app.middleware("http")
    async def _headers(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if _is_v1(request):
            response.headers["API-Version"] = API_VERSION
        else:
            route = request.scope.get("route")
            successor = LEGACY_TO_V1.get(getattr(route, "path", None))
            if successor:
                for name, value in request.path_params.items():
                    successor = successor.replace("{" + name + "}", str(value))
                logger.info("Legacy API request method=%s route=%s status=%s request_id=%s",
                            request.method, route.path, response.status_code, request.state.request_id)
                if _legacy_deprecation_enabled():
                    response.headers["Deprecation"] = "true"
                    response.headers["Sunset"] = LEGACY_SUNSET
                    response.headers["Link"] = f'<{successor}>; rel="successor-version"'
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        if _is_v1(request):
            detail = exc.detail
            if isinstance(exc, DetailedHTTPException):
                detail = {"code": exc.code, "message": exc.detail, "details": exc.details}
            return _envelope(request, exc.status_code, detail, getattr(exc, "headers", None))
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        if _is_v1(request):
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
            "legacy": {"prefix": "/api", "deprecated": True, "sunset": LEGACY_SUNSET,
                       "policy": "docs/api-versioning.md"},
            "engine": {"name": "orcaslicer", "version": m._orca_version(),
                       "binary_version": m._orcaslicer_version},
            "ready": {"catalog": m.catalog is not None and m.catalog.is_built},
            "features": ["jobs", "jobs.idempotency", "jobs.events", "pagination.cursor",
                         "profiles", "projects.inspect", "projects.subset", "projects.merge",
                         "projects.previews", "pack", "arrange", "health", "slice.prepared",
                         "slice.thumbnail", "profiles.rescan", "profiles.broken", "profiles.upload",
                         "profiles.merged_config", "profiles.neutral", "cleanup"],
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
              description="Same inputs as the legacy `POST /api/slice/start`. Send an "
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
        geometry_only_retry: Optional[str] = Form(None),
        extra_config: Optional[str] = Form(None, description="JSON object merged into project settings"),
    ):
        geometry_only_retry = m._form_default_true(geometry_only_retry)
        key =request.headers.get("Idempotency-Key")
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

    # ---- aliases: same handlers as the legacy routes, served under v1 (errors enveloped).
    # Registered before the profile routes so literal paths (e.g. /profiles/broken) win over
    # /profiles/{profile_uuid}.
    legacy = {(r.path, meth): r for r in app.routes if hasattr(r, "endpoint") and hasattr(r, "methods")
              for meth in r.methods}
    for old, method, new, tag, summary in V1_ALIASES:
        r = legacy[(old, method)]
        app.add_api_route(new, r.endpoint, methods=[method], tags=[tag], summary=summary,
                          description=r.description, responses=r.responses,
                          operation_id=f"v1_{r.name}")

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


    # Keep runtime behavior intact while advertising retirement in both specs.
    for route in app.routes:
        if getattr(route, "path", None) in LEGACY_TO_V1:
            route.deprecated = True
