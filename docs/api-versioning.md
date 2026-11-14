# Laminus API versioning

## Versions

| Version | Prefix | Status |
|---|---|---|
| v1 | `/api/v1` | Stable. The contract is `openapi-v1.json` (CI-regenerated) |
| legacy | `/api/*` (no version) | Retired October 24, 2026 (pre-alpha exception); returns 404 |

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

| v1 | Replaces (legacy) |
|---|---|
| `POST /api/v1/jobs` (202 + `Location`) | `POST /api/slice/start` |
| `GET /api/v1/jobs` | `GET /api/jobs` |
| `GET /api/v1/jobs/{id}` | `GET /api/slice/status/{id}` |
| `GET /api/v1/jobs/{id}/events` | `GET /api/slice/logs/{id}` |
| `GET /api/v1/jobs/{id}/artifact` | `GET /api/slice/download/{id}` |
| `GET /api/v1/profiles`, `GET /api/v1/profiles/{uuid}` | `GET /api/profiles`, `GET /api/profiles/{uuid}` |
| `POST /api/v1/projects/inspect` | `POST /api/3mf/objects` |
| `POST /api/v1/projects/subset` | `POST /api/arrange/subset` |
| `POST /api/v1/projects/merge` | `POST /api/arrange/merge` |

`projects/subset` and `projects/merge` accept `process_uuid` / `filament_uuids` overrides and report remapped/dropped filament slots and incompatible embedded presets in the `X-Remap-Warnings` header (see `laminus-api-for-agents.md`).

Differences worth knowing: a job record outlives its artifact (`artifact.available: false` after the one-time download or TTL eviction, instead of a 404 on the job); artifact on a not-yet-complete job is 409 (legacy: 400); profile lists are flat with `type`, filtered by `type=`/`q=` and paginated (legacy: grouped, unpaginated).

Other supported v1 endpoints (the legacy paths in this migration map now return 404):

| v1 | Replaces (legacy) |
|---|---|
| `GET /api/v1/health` | `GET /api/health` |
| `POST /api/v1/pack`, `POST /api/v1/arrange` | `POST /api/pack`, `POST /api/arrange` |
| `POST /api/v1/projects/previews` | `POST /api/3mf/previews` |
| `POST /api/v1/slice/prepared`, `POST /api/v1/slice/thumbnail` | `POST /api/slice/prepared`, `POST /api/slice/thumbnail` |
| `POST /api/v1/profiles/rescan`, `GET /api/v1/profiles/broken`, `POST /api/v1/profiles/upload` | same paths under `/api` |
| `POST /api/v1/profiles/merged-config`, `POST /api/v1/profiles/machines/suggest`, `POST /api/v1/profiles/models/variants`, `GET /api/v1/profiles/machines/{ref}/presets`, `GET /api/v1/profiles/machines/{ref}/supports` | same paths under `/api` |
| `POST /api/v1/cleanup` | `POST /api/cleanup` |

Every former legacy operation has a v1 replacement. Versionless routes are no longer registered.

## Pre-alpha retirement exception (BIZ-334 / BIZ-335)

On October 24, 2026 the owner explicitly waived the sunset window across the
repository set because the stack is pre-alpha. This supersedes the previously
agreed April 24, 2027 date and the zero-traffic waiting gate for this migration.
Versionless API routes are removed immediately in Laminus and its mock; there
are no compatibility redirects or environment flags to restore them. Upgrade
clients and the bundled UI together. Themis, Ordinus, and Concordia already use
v1 in their active clients and healthchecks.

`GET /api/v1` reports `legacy.retired: true` and `legacy.sunset: "2026-10-24"`.
`openapi.json` now contains only the v1 contract; `openapi-v1.json` remains the
version-specific artifact. Both are regenerated and checked by CI. Dashboard,
Swagger, and OpenAPI document URLs remain operational outside `/api`.

This is a breaking pre-alpha release (0.12.0). The exception does not change the
compatibility guarantees for API v1 or future stable deprecations. Future route
removals require a replacement and the normal six-month notice unless another
explicit exception is agreed.
