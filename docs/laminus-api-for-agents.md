# Laminus API — Agent Reference

Use `http://laminus:5000/api/v1` (adjust host as needed). All versionless API
routes were retired in pre-alpha 0.12.0 on October 24, 2026. See
[versioning and migration map](api-versioning.md) for the explicit sunset-window
exception. Old `/api/*` URLs return 404.

The complete request schemas, required fields, and endpoint descriptions are in
[`openapi-v1.json`](../openapi-v1.json), regenerated in CI. `/docs` serves Swagger;
`/openapi.json` serves the same v1-only API surface. UI/document URLs are not API
version aliases. Feature-detect using `GET /api/v1` rather than version strings.

## Canonical slice workflow

1. `GET /api/v1/health`: wait for `catalog_loaded: true`.
2. `GET /api/v1/profiles?type=machine&limit=200`: choose a machine UUID. List process
   and filament profiles the same way. Follow `page.next_cursor` until null;
   listings return `{data: [...], page: {limit, next_cursor}}`.
3. `POST /api/v1/jobs` with multipart `file`, `machine_uuid`, `process_uuid`,
   `filament_uuids` (a JSON array string), and `plate`. A machine may alternatively
   be identified with `manufacturer`, `model`, and `nozzle` together. Return:
   **202**, a job view with `id`, and `Location: /api/v1/jobs/{id}`.
4. Poll `GET /api/v1/jobs/{id}` or connect to
   `GET /api/v1/jobs/{id}/events`. SSE events are typed `log`/`dropped` with
   `{message}`, `completed` with `{}`, or `failed` with `{reason}`. They are not
   untyped legacy sentinel strings. Reconnect with `Last-Event-ID`.
5. `GET /api/v1/jobs/{id}/artifact` returns GCode/3MF once. The job record remains
   after download; its `artifact.available` becomes false. Artifact requests
   before completion return 409; missing artifacts return 404.

A job view has `id`, `status`, `filename`, `output_format`, `error`,
`artifact: {name, available}`, timestamps, and `links: {self, events, artifact}`.
List jobs with `GET /api/v1/jobs?limit=50&cursor=...&status=completed`.

`POST /api/v1/jobs` accepts `Idempotency-Key`. The same key/body replays the
original job; different bodies return 422 and in-flight duplicates return 409.
Keys live 24 hours in memory and do not survive restart.

Slicing always extracts the requested `plate` into its own single-plate project
(moved to plate-1 coordinates; that plate's custom layer G-code kept) and runs
Orca with `--arrange 0`, so parts never move between plates. A missing or empty
plate, or a run that produces no G-code, fails the job. `geometry_only_retry` is
opt-in (default `false`): when `true`, a failed slice is retried once without
`model_settings.config`, which drops per-object settings; the job log warns.

For an already-configured 3MF use `POST /api/v1/slice/prepared` with multipart
`file` and `plate`. This endpoint returns 200 `{job_id}`; use the same v1 job
status/events/artifact URLs afterwards. Optional `export_3mf` must be a plain
filename ending in `.3mf`, never an absolute or relative path.

## Profiles

- `GET /api/v1/profiles`: flat, typed, cursor-paginated entries, optionally filtered
  by `type` (`machine`, `process`, `filament`) or `q`. Compatibility filtering
  requires `manufacturer`, `model`, and `nozzle` together.
- `GET /api/v1/profiles/{uuid}`: one profile. UUIDs are stable identifiers;
  integrations should not send catalog filesystem paths as profile references.
- `POST /api/v1/profiles/rescan`: force a rebuild (200); poll v1 health until ready.
- `GET /api/v1/profiles/broken`: profiles with unresolved inheritance.
- `POST /api/v1/profiles/upload`: install a user profile and rebuild the catalog.
- `POST /api/v1/profiles/merged-config`: resolve machine/process/filament UUIDs.
- `POST /api/v1/profiles/machines/suggest` and
  `POST /api/v1/profiles/models/variants`: model/nozzle discovery.
- `GET /api/v1/profiles/machines/{ref}/presets` and
  `GET /api/v1/profiles/machines/{ref}/supports`: compatible presets and bed checks.

Neutral model references use `<kind>:<slug(name)>`. Responses expose catalog
version/staleness. Prefer thin user presets using `inherits` so vendor fixes can
be picked up; flatten only for permanent forks.

## Packing, arrangement, and project operations

`POST /api/v1/pack` takes multipart `files` (STL only; 3MF uploads are rejected
with 422, so use `projects/subset` or `projects/merge` to rearrange existing
projects without losing per-object data) and either a `template` 3MF,
profile UUIDs, a `machine_uuid` alone (shortcut: uses that machine's bed, no
process/filament needed; explicit `bed_*` win if also sent), or explicit `bed_x`, `bed_y`,
`bed_z`. It returns a packed 3MF.
For quantities send `items` as a JSON array string:

```json
[{"file": "part.stl", "quantity": 3}, {"file": "other.stl", "quantity": 2}]
```

Upload each unique file once, with a unique filename. The manifest must name all
uploads exactly once, quantities must be integers >= 1, and the total is capped
at 500 instances. Omitting `items` makes one copy per upload. Validation errors
include per-field `error.details`. Packing preserves each input's minimum Z and
does not tilt parts about X/Y; the arranger may rotate about Z.

`POST /api/v1/arrange` accepts a 3MF and `arrange`/`orient` flags. Orientation is
opt-in (off by default) to preserve layer direction.

Other project operations:

- `POST /api/v1/projects/inspect`: objects in a 3MF.
- `POST /api/v1/projects/previews`: render plate previews.
- `POST /api/v1/projects/subset`: select objects and quantities, arrange/retarget.
- `POST /api/v1/projects/merge`: combine selections from multiple projects.
- `POST /api/v1/slice/thumbnail`: render a configured 3MF thumbnail.
- `POST /api/v1/cleanup`: inspect/remove stale temporary directories.

Subset/merge support `process_uuid` and `filament_uuids` overrides; inspect
`X-Remap-Warnings` for dropped/remapped filament slots and incompatible embedded
presets. The OpenAPI document defines multipart field shapes and defaults.

## Errors and readiness

V1 errors, including validation and unknown v1 routes, use:

```json
{"error":{"code":"job_not_found","message":"Job not found.","status":404},"request_id":"..."}
```

Validation errors may include `error.details`. Branch on `error.code`, display
`error.message`, and retain the request ID for diagnostics. Responses include
`X-Request-ID` and `API-Version: v1`. Catalog-not-ready errors carry `Retry-After`.
Do not submit profile-dependent work until health reports the catalog loaded.
