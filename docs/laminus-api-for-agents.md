# Laminus API — Agent Reference

Base URL: `http://localhost:5000` (adjust to wherever the container is exposed)

Full OpenAPI 3.0 spec: [`openapi.json`](../openapi.json) (CI-regenerated, always matches the running code)
Live Swagger UI (container must be running): `http://localhost:5000/docs`
Live OpenAPI JSON: `http://localhost:5000/openapi.json`

> **API v1 (`/api/v1`) is the stable, versioned contract**: uniform error envelope, cursor pagination, `Idempotency-Key` on job creation, typed SSE events, `GET /api/v1` discovery. New integrations should use it; the routes below remain supported and unchanged (they will carry `Deprecation` headers where a v1 equivalent exists once `LEGACY_DEPRECATION_HEADERS` is enabled). See [`api-versioning.md`](api-versioning.md) and [`openapi-v1.json`](../openapi-v1.json).


---

## Canonical workflow

```
GET  /api/health                        → wait until catalog_loaded = true
GET  /api/profiles?manufacturer=…&model=…&nozzle=…
                                        → pick process_uuid + filament_uuids
POST /api/slice/start  (multipart)      → job_id
loop: GET /api/slice/status/{job_id}    → until status = "completed" | "failed"
GET  /api/slice/download/{job_id}       → binary GCode (or 3MF)
```

If you already have a fully-configured 3MF (OrcaSlicer-exported with settings embedded):

```
POST /api/slice/prepared  (multipart)   → job_id
... same poll/download loop
```

---

## 1 — Health check

```
GET /api/health
```

**Do not call profile or slice endpoints until `catalog_loaded` is `true`.** The catalog
builds in the background after container startup (~10–60 seconds depending on profile count).

```json
{
  "status": "healthy",
  "orcaslicer_installed": true,
  "orcaslicer_version": "OrcaSlicer-2.2.0",
  "orca_version": "2.2.0",
  "config_mounted": true,
  "system_profiles_available": true,
  "catalog_loaded": true,
  "catalog_profile_count": { "machine": 142, "process": 87, "filament": 310 },
  "active_jobs": 0
}
```

---

## 2 — Discover profiles

```
GET /api/profiles
GET /api/profiles?manufacturer=Bambu+Lab&model=P1S&nozzle=0.4
```

Without filters: returns all machines, all processes, all filaments.

With filters (supply all three or none): returns the matching machine plus only the
process and filament presets that are compatible with it.

**503 while catalog is building.** Retry.

**Response shape:**
```json
{
  "machine":  [ { ...MachineProfile }, ... ],
  "process":  [ { ...ProcessProfile }, ... ],
  "filament": [ { ...FilamentProfile }, ... ]
}
```

**MachineProfile key fields:**
| Field          | Type    | Notes                                        |
|----------------|---------|----------------------------------------------|
| `uuid`         | string  | Stable UUID — use for machine lookup         |
| `manufacturer` | string  | e.g. `"Bambu Lab"`                           |
| `model`        | string  | e.g. `"P1S"`                                 |
| `nozzle`       | string  | e.g. `"0.4"`                                 |
| `extruder_count` | int   | Number of extruders                          |

**ProcessProfile key fields:**
| Field                 | Type          | Notes                                 |
|-----------------------|---------------|---------------------------------------|
| `uuid`                | string        | Pass as `process_uuid`                |
| `layer_height`        | string\|null  | e.g. `"0.2"`                          |
| `compatible_printers` | string[]      | Empty = universal                     |

**FilamentProfile key fields:**
| Field                         | Type    | Notes                           |
|-------------------------------|---------|---------------------------------|
| `uuid`                        | string  | Pass in `filament_uuids` array  |
| `display_name`                | string  | Human-friendly name             |
| `filament_type`               | string  | `"PLA"`, `"PETG"`, etc.         |
| `filament_colour`             | string  | Hex, e.g. `"#FFFFFF"`           |
| `compatible_printers`         | string[]| Empty = universal               |
| `nozzle_temperature`          | any     | Recommended print temp (°C)     |

### Get one profile by UUID

```
GET /api/profiles/{profile_uuid}
```

Returns the full profile dict for inspection. Same 503 behaviour.

---

## 2b — Neutral lookups (model / nozzle variant → machine, presets)

Resolve profiles from neutral inputs instead of Orca preset names. Pure catalog lookups: no
engine needed, answered from the cached catalog.

**Common to all four endpoints**
- Header `X-Catalog-Version` = body `catalog_version` (opaque; changes only when preset identity or
  compatibility data changes). Revalidate stored refs when it changes.
- `"stale": true` in the body when served from an older on-disk cache (no live catalog yet) or
  while a rebuild runs. No catalog at all: **503** `{"error": "catalog_unavailable"}`.
- **Refs** are `<kind>:<slug(name)>` (lowercase, runs of non `[a-z0-9.]` → `-`), e.g.
  `machine:bambu-lab-p1s-0.4-nozzle`, `process:0.20mm-p1s`. Stable across rebuilds while the
  preset keeps its name. Every item also carries its `uuid`, which the slice endpoints take.
- `flow` is `high_flow` when the preset name contains "High Flow", else `standard`.

```
POST /api/profiles/machines/suggest   {"manufacturer": "Bambu Lab", "model": "P1S", "model_key": "p1s"}
→ {"catalog_version": "…", "candidates": [
     {"ref": "machine:bambu-lab-p1s-0.4-nozzle", "uuid": "…", "name": "Bambu Lab P1S 0.4 nozzle",
      "nozzle_diameter_mm": 0.4, "flow": "standard", "confidence": 1.0, "match_reason": "exact_model"}, … ]}
```
`match_reason`: `exact_model` | `fuzzy_name` | `vendor_only` (same vendor, other model, conf 0.1,
max 5). Sorted by confidence, then 0.4 standard first, then nozzle. Max 20. No match → `[]`, 200.

```
POST /api/profiles/models/variants    {"manufacturer": "Bambu Lab", "model": "P1S"}
→ {"catalog_version": "…", "variants": [
     {"ref": …, "uuid": …, "name": …, "nozzle_diameter_mm": 0.4, "flow": "standard", "is_default_guess": true},
     {… 0.4 high_flow, false}, {… 0.6 standard, false} ]}
```
Best-matching model only; ordered by nozzle then standard-before-high-flow. Exactly one
`is_default_guess` (0.4 standard, else smallest standard). Unknown model → `[]`.

```
GET /api/profiles/machines/{machine_ref}/presets?kind=process|filament   (default: both)
→ {"catalog_version": "…", "machine_ref": "…", "presets": [
     {"ref": "process:0.20mm-p1s", "uuid": "…", "name": "0.20mm P1S", "kind": "process",
      "compatible_models": ["Bambu Lab P1S"]}, … ]}
```
Applies inheritance, then `compatible_printers`. Empty = universal within the machine's own vendor bundle, plus user presets and the shared `OrcaFilamentLibrary` (`compatible_models` omitted
then). Abstract bases (`instantiation: false`) are excluded. `compatible_printers_condition` is not evaluated. 404 `{"error": "unknown_machine_ref"}`;
bad `kind` → 422 `{"error": "invalid_kind"}`.

```
GET /api/profiles/machines/{machine_ref}/supports?bed_x=256&bed_y=256
→ {"catalog_version": "…", "machine_ref": "…", "supported": true, "reason": null}
```
Always 200. `reason`: `unknown_machine_ref` | `bed_mismatch` (>1 mm off; a machine with no
declared bed size is never ruled out).

---

## 3a — Slice with UUID-based profile resolution

```
POST /api/slice/start
Content-Type: multipart/form-data
```

| Field              | Type    | Required | Description                                              |
|--------------------|---------|----------|----------------------------------------------------------|
| `file`             | file    | yes      | `.3mf` or `.stl` model                                  |
| `machine_uuid`     | string  | one of*  | UUID of a `machine` profile — **preferred**, stable across catalog rebuilds |
| `manufacturer`     | string  | one of*  | e.g. `"Bambu Lab"` — fallback when you don't have `machine_uuid` |
| `model`            | string  | one of*  | e.g. `"P1S"` — fallback, provide with `manufacturer` + `nozzle` |
| `nozzle`           | string  | one of*  | e.g. `"0.4"` — fallback, provide with `manufacturer` + `model` |
| `process_uuid`     | string  | yes      | UUID of a `process` profile                              |
| `filament_uuids`   | string  | yes      | JSON array of filament UUIDs, e.g. `'["uuid1"]'`         |
| `plate`            | int     | yes      | Build plate number (1-based)                             |
| `export_3mf`       | string  | no       | Output 3MF filename; omit to get GCode only              |
| `geometry_only_retry` | bool | no      | Default `true` — retry stripping model_settings on fail  |
| `extra_config`     | string  | no       | JSON object merged into project settings after profile resolution |

\* Provide `machine_uuid` **or** all three of `manufacturer` + `model` + `nozzle`.

**curl example (preferred — `machine_uuid`):**
```bash
curl -X POST http://localhost:5000/api/slice/start \
  -F "file=@model.3mf" \
  -F "machine_uuid=<uuid-from-catalog>" \
  -F 'process_uuid=<uuid-from-catalog>' \
  -F 'filament_uuids=["<uuid-from-catalog>"]' \
  -F "plate=1"
```

**curl example (fallback — manufacturer/model/nozzle):**
```bash
curl -X POST http://localhost:5000/api/slice/start \
  -F "file=@model.3mf" \
  -F "manufacturer=Bambu Lab" \
  -F "model=P1S" \
  -F "nozzle=0.4" \
  -F 'process_uuid=<uuid-from-catalog>' \
  -F 'filament_uuids=["<uuid-from-catalog>"]' \
  -F "plate=1"
```

**Response:**
```json
{ "job_id": "550e8400-...", "status": "pending", "message": "Slicing job started." }
```

**Error cases:**
- `503` — catalog not ready
- `422` — UUID not found, incompatible profiles, `plate < 1`, malformed `filament_uuids`
- `400` — bad filename

---

## 3b — Slice a pre-built 3MF

```
POST /api/slice/prepared
Content-Type: multipart/form-data
```

Use this when the `.3mf` already contains `Metadata/project_settings.config` (i.e., it
was exported from OrcaSlicer or assembled externally). No catalog lookup is performed.

| Field              | Type    | Required | Description                                    |
|--------------------|---------|----------|------------------------------------------------|
| `file`             | file    | yes      | `.3mf` with embedded print settings            |
| `plate`            | int     | yes      | Build plate number (1-based)                   |
| `export_3mf`       | string  | no       | Optional output 3MF filename                   |
| `geometry_only_retry` | bool | no      | Default `true`                                  |

**curl example:**
```bash
curl -X POST http://localhost:5000/api/slice/prepared \
  -F "file=@model_with_settings.3mf" \
  -F "plate=1"
```

---

## 4 — Poll job status

```
GET /api/slice/status/{job_id}
```

Recommended interval: every 2–5 seconds.

```json
{
  "job_id": "550e8400-...",
  "status": "completed",
  "output_format": "gcode",
  "sliced_file": "model_plate1.gcode",
  "error": null
}
```

`status` values: `pending` → `slicing` → `completed` | `failed`

`output_format`: `"gcode"` (standard) or `"gcode_3mf"` (when `export_3mf` was set —
download returns the `.3mf`).

`404` after a job was completed = job was already downloaded and evicted.

---

## 5 — Download output

```
GET /api/slice/download/{job_id}
```

Only call when `status = "completed"`. Returns `application/octet-stream`.

**Warning:** downloading evicts the job immediately. There is no second download.

```bash
curl http://localhost:5000/api/slice/download/{job_id} -o output.gcode
```

---

## (Optional) Stream logs

```
GET /api/slice/logs/{job_id}
```

Server-Sent Events. Each event: `data: <log line>\r\n\r\n`.

Stream ends with `data: __COMPLETED__` (success) or `data: __FAILED__: <reason>`.

Most agents can skip this and just poll `/api/slice/status` instead.

---

## Arrange (synchronous, no job lifecycle)

```
POST /api/arrange
Content-Type: multipart/form-data
```

| Field     | Type    | Required | Default | Description                        |
|-----------|---------|----------|---------|------------------------------------|
| `file`    | file    | yes      |         | `.3mf` to arrange                  |
| `arrange` | bool    | no       | `true`  | Run plate packing                  |
| `orient`  | bool    | no       | `false` | Run auto-orientation (may tilt parts about X/Y) |

Returns the rearranged `.3mf` as `application/octet-stream`. Timeout: 35 seconds.

```bash
curl -X POST http://localhost:5000/api/arrange \
  -F "file=@multi_model.3mf" \
  -o arranged.3mf
```

---

## Pack STLs onto plates (synchronous)

```
POST /api/pack      (also /api/v1/pack)
Content-Type: multipart/form-data
```

| Field        | Type      | Required | Description                                                        |
|--------------|-----------|----------|--------------------------------------------------------------------|
| `files`      | file[]    | yes      | 1–50 `.stl` files                                                  |
| `items`      | JSON text | no       | `[{"file": "a.stl", "quantity": 3}, ...]` — pairs each upload with a copy count |
| bed source   |           | yes      | One of `template`, `machine_uuid`+`process_uuid`+`filament_uuids`, `bed_x`+`bed_y`+`bed_z` |

Orientation: parts are never tilted about X/Y (no auto-orient; layer-line direction is preserved). The arranger may rotate about Z. Each instance keeps its input STL's min Z, so models intentionally above or below the plate stay there.

`items` omitted = 1 copy each. `file` is the uploaded filename; every upload needs exactly one entry (manifest order is irrelevant) and filenames must be unique when `items` is used. Each STL yields `quantity` arranged copies sharing one mesh in the 3MF, spread over as many plates as needed. 422 on bad JSON/shape, unknown/duplicate/missing file, quantity not an integer >= 1, or total copies > 500. The 50 cap applies to unique files.

On `/api/v1/pack` the 422 envelope lists every problem in `error.details` (`loc`, `msg`, `type`, plus `input`/`ctx` where useful), e.g. `loc: ["body","items",1,"quantity"]`. The frozen legacy route returns only a summary string in `detail`.

```bash
curl -X POST http://localhost:5000/api/pack \
  -F "files=@a.stl" -F "files=@b.stl" -F 'items=[{"file":"a.stl","quantity":3},{"file":"b.stl","quantity":1}]' \
  -F bed_x=256 -F bed_y=256 -F bed_z=256 -o packed.3mf
```

Returns `.3mf` (`application/octet-stream`). Timeout: 120 seconds.

---

## List objects with plate names and previews

```
POST /api/3mf/previews         # multipart/form-data
```

| Field    | Type | Required | Description |
|----------|------|----------|-------------|
| `file`   | file | yes      | Orca/Bambu `.3mf` |
| `size`   | int  | no       | Preview edge in px, 32-1024 (default 256) |
| `images` | bool | no       | `false` = names/plates only, no rendering (fast) |

```json
{"objects": [{
  "id": 8, "name": "insert", "extruder": "1",
  "plates": [{"plate": 3, "name": "regular pot", "instances": 1}],
  "preview": {"mime": "image/png", "width": 256, "height": 256, "data_base64": "iVBORw0..."}
}]}
```

`plates[].name` is the plate's name in the project, or `Plate N` when unnamed; an object on several plates lists each. `preview` renders **the object itself** (3/4 view, transparent background, tinted with its filament colour, modifier parts omitted), or is `null` if nothing printable. Use `id` in `/api/arrange/subset` / `/api/arrange/merge` selections. Rendering is CPU-bound (~0.5 s/object; a 21-object project ~12 s) - use `images=false` when only names are needed. Requires `numpy` (in `requirements.txt`).

---

## Arrange a subset (pick objects × qty from a 3MF)

```
POST /api/v1/projects/inspect  # list objects: id, name, extruder, override keys, parts, instances (legacy alias: /api/3mf/objects)
POST /api/v1/projects/subset   # multipart/form-data (legacy alias: /api/arrange/subset)
```

| Field             | Type   | Required | Description |
|-------------------|--------|----------|-------------|
| `file`            | file   | yes      | Orca/Bambu `.3mf` |
| `selection`       | string | yes      | JSON `[{"name":"Lid","qty":4},{"id":12,"qty":1}]`. A name shared by several objects → 422, use `id` |
| `machine_uuid`    | string | no       | Retarget bed/printer keys to this machine preset (catalog UUID) |
| `process_uuid`    | string | no       | Replace the project's process preset with this one (needs `machine_uuid`) |
| `filament_uuids`  | string | no       | JSON array of filament preset UUIDs, one per slot (needs `machine_uuid`; more than the machine's slots → 422) |
| `allow_rotations` | bool   | no       | Pass `--allow-rotations` to the arranger |

Returns a `.3mf` with only the selected objects (qty copies each, as instances) arranged over as many plates as OrcaSlicer needs. Headers: `X-Plate-Count`, `X-Instance-Count`, `X-Warnings` (objects not resting on the bed; empty when fine). Per-object/part overrides, modifier parts, height ranges and painted data are preserved; with `machine_uuid`, filament slots are cut to what the machine supports (`nozzle_diameter` count, unless `single_extruder_multi_material`): per-slot settings are truncated and objects/parts/height ranges on a dropped slot move to extruder 1. Painted colours are not remapped. `process_uuid` / `filament_uuids` replace the embedded presets; otherwise embedded presets whose `compatible_printers` exclude the target are reported. `X-Remap-Warnings` lists everything remapped/dropped/incompatible. See `docs/experiments/subset-arrange.md`.

```bash
curl -X POST http://localhost:5000/api/v1/projects/subset \
  -F "file=@project.3mf" \
  -F 'selection=[{"name":"Lid","qty":6},{"id":12,"qty":2}]' \
  -F "machine_uuid=8aaac37c-14a7-5a53-b9f7-3dd1df210919" \
  -o subset.3mf -D -
```

---

## Merge selections from several 3MFs into one

```
POST /api/v1/projects/merge    # multipart/form-data, repeated `files` parts (legacy alias: /api/arrange/merge)
```

| Field             | Type   | Required | Description |
|-------------------|--------|----------|-------------|
| `files`           | file[] | yes      | Two or more `.3mf`. **The first upload is the base**: its process/filament/printer settings, thumbnails and metadata are used for the whole result |
| `selection`       | string | yes      | JSON list. Each entry names its source with `file` (uploaded filename, or 0-based upload index) plus `id` / `name` / `plate` and `qty` |
| `machine_uuid`    | string | no       | Retarget bed/printer keys (as in `/api/arrange/subset`) |
| `process_uuid`    | string | no       | Replace the project's process preset with this one (needs `machine_uuid`) |
| `filament_uuids`  | string | no       | JSON array of filament preset UUIDs, one per slot (needs `machine_uuid`; more than the machine's slots → 422) |
| `allow_rotations` | bool   | no       | Pass `--allow-rotations` to the arranger |

`{"plate": N, "qty": q}` = everything on source plate N (each object x its count there x q); it also works in `/api/arrange/subset`. Entries for the same object accumulate.

Response: one `.3mf`. Headers `X-Plate-Count`, `X-Instance-Count`, `X-Remap-Warnings` (as in subset, prefixed by source file) and `X-Merge-Warnings` (non-empty when a non-base source used a different process/filament preset, has painted data with a different filament count, or an object ends up not resting on the bed - floating/sunk by more than 0.5 mm). Errors: 422 (bad selection / unknown file / base not selected / non-base object uses a filament slot the base lacks), 400, 408, 503.

```bash
curl -X POST http://localhost:5000/api/arrange/merge   -F "files=@Hex-Moss-Pole.3mf"   -F "files=@cloud pots.3mf"   -F 'selection=[
        {"file":"Hex-Moss-Pole.3mf","plate":1,"qty":2},
        {"file":"Hex-Moss-Pole.3mf","plate":6},
        {"file":"Hex-Moss-Pole.3mf","plate":7},
        {"file":"cloud pots.3mf","plate":3,"qty":2},
        {"file":"cloud pots.3mf","id":8,"qty":1}]'   -F "machine_uuid=0f0a771f-0a29-5077-8571-e1fee605f432"   -o merged.3mf -D -
```

---

## Upload a user profile

```
POST /api/profiles/upload
Content-Type: multipart/form-data
```

| Field  | Type   | Values                      |
|--------|--------|-----------------------------|
| `type` | string | `machine`, `process`, `filament` |
| `file` | file   | Flat OrcaSlicer preset JSON |

Triggers a catalog rebuild. Wait ~5 seconds, then call `GET /api/profiles?refresh=false`
to confirm the new profile appears.

---

## Error response shape

All JSON error responses have a single `detail` field:

```json
{ "detail": "Human-readable error message." }
```

The 503 "catalog building" response is the exception:
```json
{ "status": "building_catalog", "detail": "Catalog not ready. Retry shortly." }
```

---

## UUID stability

Profile UUIDs are deterministic (UUID5) and do not change across container restarts:

- **Machine UUIDs** are derived from `(manufacturer, model, nozzle)`.
- **Process / filament UUIDs** are derived from `(source, rel_path)`.

This means a UUID discovered in one session is valid in the next session without
re-querying the catalog, as long as the profile files haven't changed.
