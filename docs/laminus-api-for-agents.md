# Laminus API — Agent Reference

Base URL: `http://localhost:5000` (adjust to wherever the container is exposed)

Full OpenAPI 3.0 spec: [`docs/laminus-openapi.yaml`](laminus-openapi.yaml)
Live Swagger UI (container must be running): `http://localhost:5000/docs`
Live OpenAPI JSON: `http://localhost:5000/openapi.json`

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

## 3a — Slice with UUID-based profile resolution

```
POST /api/slice/start
Content-Type: multipart/form-data
```

| Field              | Type    | Required | Description                                              |
|--------------------|---------|----------|----------------------------------------------------------|
| `file`             | file    | yes      | `.3mf` or `.stl` model                                  |
| `manufacturer`     | string  | yes      | e.g. `"Bambu Lab"`                                       |
| `model`            | string  | yes      | e.g. `"P1S"`                                             |
| `nozzle`           | string  | yes      | e.g. `"0.4"`                                             |
| `process_uuid`     | string  | yes      | UUID of a `process` profile                              |
| `filament_uuids`   | string  | yes      | JSON array of filament UUIDs, e.g. `'["uuid1"]'`         |
| `plate`            | int     | yes      | Build plate number (1-based)                             |
| `export_3mf`       | string  | no       | Output 3MF filename; omit to get GCode only              |
| `geometry_only_retry` | bool | no      | Default `true` — retry stripping model_settings on fail  |

**curl example:**
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
| `orient`  | bool    | no       | `true`  | Run auto-orientation               |

Returns the rearranged `.3mf` as `application/octet-stream`. Timeout: 35 seconds.

```bash
curl -X POST http://localhost:5000/api/arrange \
  -F "file=@multi_model.3mf" \
  -o arranged.3mf
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
