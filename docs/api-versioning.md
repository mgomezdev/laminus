# Laminus API versioning

## Versions

| Version | Prefix | Status |
|---|---|---|
| v1 | `/api/v1` | Stable. The contract is `openapi-v1.json` (CI-regenerated) |

**Every API route is under `/api/v1`; that is the contract.** **Interim, deprecated:** unversioned `/api/<x>` is rewritten server-side to `/api/v1/<x>` when a v1 route exists (same handler, same v1 data shape, any method; not in the OpenAPI spec) so clients can migrate; it will be removed in a future release, so call `/api/v1` directly. Unversioned paths with no v1 equivalent, e.g. the old `/api/slice/*`, `/api/arrange/{subset,merge}`, `/api/3mf/*`, are a 404 in the standard error envelope. The web UI (`/`) is the only other non-versioned path. Anything else under `/api` (including the old `/api/slice/*`, `/api/jobs`, `/api/profiles`, `/api/arrange/*`, `/api/3mf/*`) is a 404 in the standard error envelope.

`GET /api/v1` is the discovery document: supported versions, engine, readiness, `features` flags and limits. Feature-detect from `features`; don't parse version strings.

## What v1 guarantees

Within a major version, only **compatible** changes ship:

* new endpoints, new optional request params, new response fields, new enum values in *responses*, new error `code`s
* clients must ignore unknown response fields

These need a new major (`/api/v2`):

* removing/renaming an endpoint, field or error code
* new **required** request param or body field
* removing a documented response status
* changing a field's type or meaning, or changing pagination/envelope shape

`tests/test_api_v1_compat.py` enforces this against the committed baseline `tests/fixtures/api_v1_surface.json`. If it fails you are making a breaking change. To add to v1 deliberately, run `python scripts/snapshot_v1_surface.py` and commit the updated baseline.

## Conventions (v1)

**Errors.** Every v1 error, including validation (422) and unknown `/api/v1/*` paths (404):

```json
{"error": {"code": "job_not_found", "message": "Job not found.", "status": 404},
 "request_id": "…"}
```

`code` is stable and machine-readable (branch on it, not on `message`). `details` is present only for validation errors (the field-error list). 503 for "catalog not ready" carries `Retry-After`. `request_id` equals the `X-Request-ID` response header; send your own `X-Request-ID` to correlate.

**Pagination.** List endpoints take `limit` (1-200, default 50) and `cursor`, and return `{"data": [...], "page": {"limit": N, "next_cursor": "…"|null}}`. Cursors are opaque keyset cursors: new items arriving mid-walk never shift or duplicate pages. A malformed cursor is 422 `invalid_cursor`.

**Idempotency.** `POST /api/v1/jobs` honours `Idempotency-Key` (1-255 chars). Same key + same request → the original job again (`Idempotent-Replay: true`); same key + different request → 422 `idempotency_key_reuse`; first request still running → 409 `idempotency_in_flight`. A request that fails validation does not consume the key. Keys live 24 h **in memory** (cleared on restart).

**Events.** `GET /api/v1/jobs/{id}/events` is SSE with sequential `id:` and typed events `log`, `dropped`, `completed`, `failed`. Reconnect with `Last-Event-ID: N` to skip events already seen.

**Headers.** Every response carries `X-Request-ID`; v1 responses carry `API-Version: v1`.

## Resource map

| Route | Notes |
|---|---|
| `GET /api/v1` | capability discovery |
| `GET /api/v1/health` | readiness, catalog status, active jobs |
| `POST /api/v1/jobs` (202 + `Location`), `GET /api/v1/jobs`, `GET /api/v1/jobs/{id}`, `GET /api/v1/jobs/{id}/events`, `GET /api/v1/jobs/{id}/artifact` | slice job lifecycle |
| `POST /api/v1/slice/prepared`, `POST /api/v1/slice/thumbnail` | slice a pre-built 3MF / render a plate thumbnail |
| `GET /api/v1/profiles`, `GET /api/v1/profiles/{uuid}` | flat, filtered (`type=`, `q=`), cursor-paginated |
| `POST /api/v1/profiles/{merged-config,upload,rescan}`, `GET /api/v1/profiles/broken` | profile tooling |
| `POST /api/v1/profiles/machines/suggest`, `POST /api/v1/profiles/models/variants`, `GET /api/v1/profiles/machines/{ref}/{presets,supports}` | neutral model/nozzle lookups |
| `POST /api/v1/projects/inspect`, `POST /api/v1/projects/previews`, `POST /api/v1/projects/subset`, `POST /api/v1/projects/merge` | 3MF project tools |
| `POST /api/v1/pack`, `POST /api/v1/arrange` | STL packing / 3MF re-arrange |
| `POST /api/v1/cleanup` | remove stale temp dirs |

`projects/subset` and `projects/merge` accept `process_uuid` / `filament_uuids` overrides and report remapped/dropped filament slots and incompatible embedded presets in the `X-Remap-Warnings` header (see `laminus-api-for-agents.md`).

A job record outlives its artifact (`artifact.available: false` after the one-time download or TTL eviction, instead of a 404 on the job).

## Deprecation policy

* A v1 endpoint is deprecated only after its replacement in a newer major version is released, and stays for at least one full major cycle.
* Deprecated endpoints answer with `Deprecation: true` and `Link: <successor>; rel="successor-version"`. A `Sunset` header (RFC 8594) is added only once a removal date is decided, at least **6 months** ahead.
