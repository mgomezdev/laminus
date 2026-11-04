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
plate, or a run that produces no G-code, fails the job.

Plate isolation across beds (`POST /api/v1/jobs` with a 3MF; feature
`slice.plate_isolation`): the plate is located using the uploaded project's own
`printable_area` (the bed it was laid out/packed for; the target machine's bed is
used only if the project has none), then moved from the source bed centre to the
target bed centre, so the same plate N yields the same objects and the same
relative layout on any printer. If the footprint fits but that placement would
overflow, the footprint itself is centred. The printed footprint (build items ×
transforms; modifier/negative/blocker/enforcer parts ignored) is checked against
the target `printable_area` and `printable_height`; a misfit fails the job before
Orca runs with `error` like `Plate 2 footprint 300.0 × 210.0 × 120.0 mm does not
fit the target bed 256 × 256 × 256 mm`. STL uploads and `/slice/prepared` are
unaffected (prepared projects already carry their target bed). `geometry_only_retry` is
opt-in (default `false`): when `true`, a failed slice is retried once without
`model_settings.config`, which drops per-object settings; the job log warns.

For an already-configured 3MF use `POST /api/v1/slice/prepared` with multipart
`file` and `plate`. This endpoint returns 200 `{job_id}`; use the same v1 job
status/events/artifact URLs afterwards. Optional `export_3mf` must be a plain
filename ending in `.3mf`, never an absolute or relative path. Every slice input has
its `filament_ids` repaired on the way in (a stray preset-level `filament_id` is
moved or dropped, and a missing or empty list is filled with `""` per filament):
OrcaSlicer 2.4.2 segfaults in `--export-3mf` without it.

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

### Plate manifest (feature `pack.manifest`)

`/pack`, `/projects/subset` and `/projects/merge` embed
`Metadata/laminus_manifest.json` in the returned 3MF:

```json
{"version": 1,
 "items": [{"item": 0, "source": "c.stl", "requested": 7}],
 "plates": [{"plate": 1, "instances": [
   {"item": 0, "source": "c.stl", "object_id": "16", "object_name": "c", "part": "c.stl"}]}]}
```

- `item`: 0-based index of the `/pack` `items` entry (upload order when `items` is
  omitted) or of the subset/merge `selection` entry.
- `source`: uploaded filename. `object_id`/`object_name`: the object in the returned 3MF.
- `part`: stable key within the item: the STL filename (pack) or the source 3MF's
  object id as a string (subset/merge; a `plate` selector yields several parts).
- `requested` = qty × parts; per item, instances summed over plates always equal it
  (otherwise the request fails with 500 instead of returning a wrong manifest).
- `plate` is the plater_id: `POST /api/v1/jobs` with `plate=N` slices exactly that
  plate's instances. When two selection entries pick the same object, its instances
  are attributed in selection order.

### Next plate (feature `pack.next`)

`POST /api/v1/pack/next` returns the fullest single plate one machine can print
from a remaining-parts selection. Multipart: repeated `files` (`.3mf` and/or
`.stl`), `machine_uuid` (required), optional `max_parts`, and `selection` shaped
like `/projects/merge`, plus an optional `rule` per entry:

```json
[{"file": "a.3mf", "plate": 2, "qty": 1, "rule": "together"},
 {"file": "a.3mf", "id": 8, "qty": 3},
 {"file": "b.stl", "qty": 6}]
```

- An STL entry is `{file, qty}` only.
- `split` (default): instances are placed independently.
- `together`: each copy of the entry (e.g. one plate bundle) is all-or-nothing on
  the returned plate. Orca chooses the copy's layout; the authored layout is not kept.
- `solo` is rejected with 422; send those as their own jobs.
- Orca arranges the selection for the machine's bed and plate 1 is taken. Parts
  larger than the bed are left out up front. A `together` copy Orca split is dropped;
  over `max_parts`, whole units are dropped, last placed first. After any drop the
  kept instances are arranged again.
- **200**: a single-plate `.3mf` with the footprint centred on the bed, ready for
  `POST /api/v1/jobs` `plate=1`. Its manifest (schema above) has
  `items[].requested` (asked) and `items[].consumed` (on this plate). Header
  `X-Consumed` is a JSON array of consumed instances indexed by selection entry,
  e.g. `[4,3,2]`.
- **422 with `error.code` `nothing_fits`**: nothing fits this machine's bed.
  `error.details[].loc` is `["body", "selection", <entry>]` for each entry whose
  parts are too big. No other failure uses this code; bad input (including `solo`)
  is a 422 with code `unprocessable`.

### Mixed multi-plate pack (feature `pack.mixed`)

`POST /api/v1/pack/mixed` arranges a mixed selection on the fewest plates of one
bed. Same `files` and `selection` as `/pack/next` (`rule` per entry), plus the bed:
either `bed_x` + `bed_y` + `bed_z` (mm; printer-less, only the projects' bed is
replaced) or `machine_uuid` (bed and printer retargeted), never both.

- `split` (default): instances go where OrcaSlicer's arrange puts them.
- `together`: each copy (one `plate` bundle) lands whole on one plate; Orca chooses
  its layout. Copies Orca split are taken off and arranged again; one that still
  splits on its own is `part_too_large`.
- `solo`: each instance alone on its own plate, footprint centred, placed after the
  arranged plates.
- **200**: one multi-plate `.3mf`; slice each plate with `POST /api/v1/jobs`
  `plate=N`. 3MF objects keep per-object overrides, modifiers and paint; project
  settings come from the first selected `.3mf` (as `/projects/merge`). Manifest
  (schema above): `item` = selection index, `part` = source object id or STL
  filename, `requested` = qty × parts, always fully placed. Headers
  `X-Plate-Count`, `X-Instance-Count`, `X-Merge-Warnings`.
- Fewest plates is Orca's arrange plus the together repair, not a global optimum:
  a re-arranged copy goes on a new plate rather than into gaps it left.
- **422 with `error.code` `part_too_large`**: a part bigger than the bed, or a
  `together` copy bigger than one plate. `error.details[].loc` is
  `["body", "selection", <entry>]` per offending entry. Other input errors are 422
  `unprocessable`.

### Measure (feature `projects.measure`)

`POST /api/v1/projects/measure` returns bounding-box sizes for fit checks. Multipart
`file` (`.3mf` or `.stl`) and optional `selection`, a JSON list of `{"id": n}` /
`{"name": s}` (one object) or `{"plate": n}` (one plate); omitted or `[]` measures
the whole file.

```json
{"items": [{"size_mm": [70.0, 60.0, 10.0]}, {"size_mm": [150.0, 60.0, 30.0]}]}
```

- One item per selection entry, in order (one for the whole file).
- Axis-aligned box of printed parts (modifier / negative / blocker / enforcer parts
  ignored), after the build transform. An object is its first instance as placed,
  so a rotated object reports its placed size. A plate is every instance on it as
  authored. A whole 3MF is every build item; a multi-plate project needs `plate`
  selectors. An STL is its mesh extents and takes no selection.
- 422 `unprocessable`: unreadable file, bad selection, unknown or ambiguous object,
  missing or empty plate.

Machine catalog entries (`GET /api/v1/profiles?type=machine`, `/profiles/{uuid}`) carry
`bed_size_z`: the machine's `printable_height` in mm (`null` when the preset does not
declare one), alongside `bed_size_x` / `bed_size_y`.

## Errors and readiness

V1 errors, including validation and unknown v1 routes, use:

```json
{"error":{"code":"job_not_found","message":"Job not found.","status":404},"request_id":"..."}
```

Validation errors may include `error.details`. Branch on `error.code`, display
`error.message`, and retain the request ID for diagnostics. Responses include
`X-Request-ID` and `API-Version: v1`. Catalog-not-ready errors carry `Retry-After`.
Do not submit profile-dependent work until health reports the catalog loaded.
