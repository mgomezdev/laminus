# Themis Integration Requirements

Requirements for the orca container API to serve as a drop-in replacement for Themis's direct OrcaSlicer subprocess calls, and as the authoritative source of profile/preset information.

---

## Architecture Overview

The integration splits responsibility cleanly along a geometry/settings boundary:

| Concern | Owner | Rationale |
|---------|-------|-----------|
| Profile catalog, inheritance resolution, preset merging | **This container** | Binary and profile files co-locate here |
| Profile selection UI, UUID storage in DB | **Themis** | Themis owns its own data model |
| Model geometry (STL/3MF source), per-object overrides | **Themis** | Model state lives in Themis's library |
| Tool/extruder remapping in model_settings.config | **Themis** | Printer-specific, requires Themis's printer knowledge |
| OrcaSlicer subprocess execution | **This container** | Binary lives here |
| Thumbnail injection, gcode filename construction | **Themis** | Applied post-download |
| Printer connection, queue management, print start | **Themis** | Out of scope for this container |

**The key shift from the previous design:** the container was originally expected to only run the binary while Themis owned all profile resolution and 3MF preparation. The updated design moves profile resolution and project config embedding into the container so that:
- Themis never needs direct filesystem access to OrcaSlicer's profile directories
- Themis stores stable profile UUIDs rather than profile paths or raw profile content
- The container's profile catalog drives the UI — Themis does not bake in profile names

---

## What Themis Sends to Slice

Themis prepares the geometry side of the 3MF and sends it with profile UUID references. The container resolves those UUIDs into a flattened `project_settings.config`, embeds it into the 3MF, and invokes OrcaSlicer.

```
Themis                                Container
  │                                       │
  │── POST /api/slice/start ─────────────>│
  │     file: model.3mf (geometry +       │
  │           model_settings.config)      │── resolve (manufacturer, model, nozzle)
  │     manufacturer: "Bambu Lab"         │── flatten profiles
  │     model: "P1S"                      │── build project_settings.config
  │     nozzle: "0.4"                     │── embed into 3MF
  │     process_uuid: "..."               │── orcaslicer --slice ...
  │     filament_uuids: ["...", "..."]    │
  │     plate: 1                          │
  │     export_3mf: (optional)            │
  │<── { job_id, status } ───────────────│
  │                                       │
  │── GET /api/slice/logs/{job_id} ──────>│── SSE log stream
  │── GET /api/slice/download/{job_id} ──>│── .gcode or .gcode.3mf
```

The model file Themis sends:
- Contains geometry (3D models) and `Metadata/model_settings.config` (per-object overrides, paint, modifiers, tool routing after remap) already applied
- Does **not** contain `Metadata/project_settings.config` — that is the container's job to generate and embed
- May be a bare STL if no per-object settings are needed (container wraps it into a 3MF internally)

---

## Functional Requirements

---

### PROFILE CATALOG

---

#### REQ-PROFILES-01 — Stable Profile UUIDs

Every profile exposed by the API must carry a stable, unique `uuid` field. The UUID is deterministic: computed as UUID5 from a fixed namespace and the string `"{source}:{rel_path}"` (e.g., `"system:Bambu Lab/filament/Bambu PLA Basic.json"`). This makes UUIDs reproducible across container restarts and image rebuilds as long as profile files do not move.

User profiles added to `/config/user/` also get stable UUIDs using the same scheme with `source = "user"`.

---

#### REQ-PROFILES-02 — Profile Catalog Endpoint

**Endpoint:** `GET /api/profiles`

Returns the full catalog of available profiles grouped by type. Metadata in the catalog **must reflect resolved (flattened) values**, not raw inherited values — so `filament_type`, `filament_colour`, etc. come from the merged chain, not the leaf file that may only have an `inherits` key.

**Machine profiles are identified by a `(manufacturer, model, nozzle)` tuple**, not a single opaque name. These three fields are the natural key for machine selection (manufacturer → model → nozzle is the standard OrcaSlicer UI drill-down). A UUID is also included for stable DB storage in Themis. The UUID for machine profiles is derived as UUID5 from the tuple string `"{manufacturer}|{model}|{nozzle}"` rather than the file path, so it survives profile directory restructuring.

**Response shape:**

```json
{
  "machine": [
    {
      "uuid": "a1b2c3d4-...",
      "manufacturer": "Bambu Lab",
      "model": "P1S",
      "nozzle": "0.4",
      "name": "Bambu Lab P1S 0.4 nozzle",
      "source": "system",
      "nozzle_diameter": 0.4,
      "bed_size_x": 256,
      "bed_size_y": 256,
      "extruder_count": 1
    },
    {
      "uuid": "a1b2c3d5-...",
      "manufacturer": "Creality",
      "model": "Ender-3",
      "nozzle": "0.4",
      "name": "Creality Ender-3 0.4 nozzle",
      "source": "user",
      "nozzle_diameter": 0.4,
      "bed_size_x": 220,
      "bed_size_y": 220,
      "extruder_count": 1
    }
  ],
  "process": [
    {
      "uuid": "b2c3d4e5-...",
      "name": "0.20mm Standard @BBL X1E",
      "source": "system",
      "layer_height": 0.2,
      "compatible_printers": ["Bambu Lab P1S 0.4 nozzle", "Bambu Lab X1E 0.4 nozzle"]
    }
  ],
  "filament": [
    {
      "uuid": "c3d4e5f6-...",
      "name": "Bambu PLA Basic @BBL X1E 0.4 nozzle",
      "display_name": "Bambu PLA Basic",
      "source": "system",
      "filament_type": "PLA",
      "filament_colour": "#FFFFFF",
      "filament_vendor": "Bambu Lab",
      "filament_diameter": 1.75,
      "compatible_printers": ["Bambu Lab P1S 0.4 nozzle"]
    }
  ]
}
```

The catalog covers both system profiles (`/opt/orcaslicer/resources/profiles/`) and user profiles (`/config/user/`). The `source` field is `"system"` or `"user"`.

**Manufacturer/model/nozzle extraction:** For system profiles, these are parsed from the directory structure and profile `name` field. The system profile tree is organized as `{Manufacturer}/machine/{Manufacturer} {Model} {nozzle_size} nozzle.json`. For user-supplied profiles, the container parses the same convention from the `name` field inside the JSON; if the name does not match the pattern, `manufacturer` and `model` fall back to `null` and the profile is still included (for hand-crafted presets).

**Caching:** The catalog is built once at startup and cached in memory. It is invalidated and rebuilt when a profile is added via the upload endpoint. A `?refresh=true` query parameter forces a fresh build.

---

#### REQ-PROFILES-03 — Profile Detail by UUID

**Endpoint:** `GET /api/profiles/{uuid}`

Returns the full flattened (resolved) data for any profile by UUID, regardless of type. The response includes all fields from the merged inheritance chain, plus the catalog metadata fields.

The `type` field in the response (`"machine"` | `"process"` | `"filament"`) allows callers to know which schema to expect without prior knowledge.

**Response (filament example):**

```json
{
  "uuid": "c3d4e5f6-...",
  "type": "filament",
  "name": "Bambu PLA Basic @BBL X1E 0.4 nozzle",
  "display_name": "Bambu PLA Basic",
  "source": "system",
  "filament_type": "PLA",
  "filament_colour": "#FFFFFF",
  "filament_vendor": "Bambu Lab",
  "filament_diameter": 1.75,
  "filament_density": 1.24,
  "nozzle_temperature_initial_layer": 220,
  "nozzle_temperature": 220,
  "nozzle_temperature_range_low": 190,
  "nozzle_temperature_range_high": 240,
  "bed_temperature_initial_layer": 35,
  "bed_temperature": 35,
  "compatible_printers": ["Bambu Lab P1S 0.4 nozzle", "Bambu Lab X1E 0.4 nozzle"],
  "compatible_printers_condition": ""
}
```

**Response (machine example):**

```json
{
  "uuid": "a1b2c3d4-...",
  "type": "machine",
  "name": "Bambu Lab P1S 0.4 nozzle",
  "source": "system",
  "nozzle_diameter": [0.4],
  "bed_size_x": 256,
  "bed_size_y": 256,
  "bed_size_z": 256,
  "extruder_count": 1,
  "machine_start_gcode": "...",
  "machine_end_gcode": "..."
}
```

Returns 404 if the UUID is not found.

---

#### REQ-PROFILES-04 — Filament Profile Fields

The detail response for filament profiles (`type: "filament"`) MUST include at minimum these fields, resolved from the full inheritance chain:

| Field | Type | Description |
|-------|------|-------------|
| `display_name` | string | Human-readable name without printer suffix (stripped from `name` at `" @"`) |
| `filament_type` | string | Material class: `"PLA"`, `"PETG"`, `"ABS"`, `"TPU"`, `"ASA"`, etc. |
| `filament_colour` | string | Hex color string, e.g. `"#FFFFFF"` (first value if the raw field is an array) |
| `filament_vendor` | string | Brand name, e.g. `"Bambu Lab"`, `"Generic"` |
| `filament_diameter` | float | Wire diameter in mm (1.75 or 2.85) |
| `filament_density` | float | g/cm³ |
| `nozzle_temperature` | int | Target print temperature °C |
| `nozzle_temperature_range_low` | int | Minimum usable temperature |
| `nozzle_temperature_range_high` | int | Maximum usable temperature |
| `bed_temperature` | int | Target bed temperature °C |
| `compatible_printers` | list[str] | Machine profile names this filament is certified for |

**Rationale:** Themis's UI displays filament color swatches, type badges, and temperature ranges when the user configures which filament is loaded in which AMS tray. These fields must be resolved (not `null` due to uninherited values) for that display to work correctly.

---

#### REQ-PROFILES-05 — Compatible Preset Filtering

**Endpoint:** `GET /api/profiles?manufacturer=<m>&model=<m>&nozzle=<n>`

When the machine tuple is provided, the catalog response filters `process` and `filament` entries to only those whose `compatible_printers` list includes the matching machine's `name`. This mirrors OrcaSlicer's own compatibility gating and prevents Themis from offering process/filament profiles that would fail to slice on the selected printer.

All three tuple fields (`manufacturer`, `model`, `nozzle`) must be provided together; providing a partial tuple returns a 422. Filtering is performed on the resolved `compatible_printers` field (after inheritance). An entry with an empty `compatible_printers` list is considered universally compatible.

The machine list in the response is unfiltered regardless of which tuple params are passed — clients always receive the full machine catalog and filter locally for the manufacturer → model → nozzle drill-down UI.

---

#### REQ-PROFILES-06 — User Profile Upload (existing, unchanged)

`POST /api/profiles/upload` — accepts a user-supplied `.json` preset file, places it under `/config/user/default/{type}/`, and invalidates the catalog cache.

---

---

### SLICING

---

#### REQ-SLICE-01 — UUID-Based Slice Endpoint

**Endpoint:** `POST /api/slice/start` (revised from current implementation)

Accept a source model file plus profile UUIDs. The container resolves and flattens the named profiles, builds `project_settings.config`, embeds it into the model 3MF, and invokes OrcaSlicer.

**Request (multipart/form-data):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | yes | Source model: `.stl` or `.3mf`. A 3MF may contain `model_settings.config` (per-object overrides, tool routing) but must NOT contain `project_settings.config` |
| `manufacturer` | string | yes | Machine manufacturer, e.g. `"Bambu Lab"` — must match a `manufacturer` value in the machine catalog |
| `model` | string | yes | Machine model, e.g. `"P1S"` |
| `nozzle` | string | yes | Nozzle size string, e.g. `"0.4"` |
| `process_uuid` | string | yes | UUID from `/api/profiles` for the process preset |
| `filament_uuids` | string (JSON array) | yes | Ordered array of filament UUIDs, one per extruder slot: `["uuid-slot-1", "uuid-slot-2"]` |
| `plate` | int | yes | Plate index, 1-based |
| `export_3mf` | string | no | If provided, passes `--export-3mf <value>` to OrcaSlicer; download returns `.gcode.3mf` (Bambu printers) |
| `geometry_only_retry` | bool | no | Default `true`; retry without `model_settings.config` on first failure |

The machine is resolved from the `(manufacturer, model, nozzle)` tuple, not a UUID, because this is the natural selection key in Themis's UI. The container looks up the matching machine entry in the catalog and returns 422 with a clear message if no match is found.

**Container behavior:**

1. Resolve machine from `(manufacturer, model, nozzle)` tuple; return 422 if not found
2. Validate `process_uuid` and all `filament_uuids` exist; return 422 with descriptive error if any is unknown
3. Validate that process and filament presets are compatible with the resolved machine (`compatible_printers` check); return 422 if not
4. Flatten the machine, process, and all filament presets (resolve full inheritance chains)
5. Build `project_settings.config` by merging the flattened presets (machine → process → per-filament)
6. If the source file is an STL, wrap it into a minimal 3MF
7. Embed the generated `project_settings.config` into the 3MF; preserve any existing `model_settings.config`
8. Run OrcaSlicer:
   ```
   xvfb-run orcaslicer --slice <plate> --outputdir <dir> --arrange 1 [--export-3mf <val>] <prepared.3mf>
   ```
9. Return `{"job_id": "...", "status": "pending"}` after file ingestion; subprocess runs in background

**Returns:** HTTP 200 `{job_id, status: "pending"}` or HTTP 422 on unresolvable machine tuple, unknown UUIDs, or compatibility mismatch.

---

#### REQ-SLICE-02 — Pre-Configured 3MF Endpoint (advanced / backward-compat)

**Endpoint:** `POST /api/slice/prepared`

For callers that have already embedded `project_settings.config` into the 3MF themselves (current Themis behavior during migration, or standalone use). The container runs OrcaSlicer directly without any profile resolution.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | yes | A `.3mf` with embedded `project_settings.config` |
| `plate` | int | yes | Plate index, 1-based |
| `export_3mf` | string | no | Same semantics as REQ-SLICE-01 |
| `geometry_only_retry` | bool | no | Default `true` |

This endpoint exists to support a phased migration: Themis can adopt the profile catalog (REQ-PROFILES-01 through REQ-PROFILES-05) while continuing to prepare 3MFs itself, then migrate to REQ-SLICE-01 in a second phase.

---

#### REQ-SLICE-03 — Dual Output Format

`GET /api/slice/download/{job_id}` serves the correct artifact based on how the job was submitted:
- No `export_3mf` → serves the first `.gcode` file in the output directory
- `export_3mf` provided → serves the named `.gcode.3mf` archive

Job status (`GET /api/slice/status/{job_id}`) includes `"output_format": "gcode"` or `"output_format": "gcode_3mf"`.

---

#### REQ-SLICE-04 — Geometry-Only Retry

When OrcaSlicer exits non-zero on a first attempt, if `geometry_only_retry=true` (default), the container strips `Metadata/model_settings.config` from the 3MF and retries. The retry is transparent: same job ID, same log stream (with a log line announcing the retry). The job fails only if both attempts fail.

---

#### REQ-SLICE-05 — Configurable Timeout

Environment variable `SLICE_TIMEOUT_SECONDS` (default `600`). OrcaSlicer is SIGKILL'd after this duration; the job transitions to `failed`.

---

### JOB MANAGEMENT

---

#### REQ-JOB-01 — Status Polling

`GET /api/slice/status/{job_id}`

```json
{
  "job_id": "...",
  "status": "pending | slicing | completed | failed",
  "output_format": "gcode | gcode_3mf",
  "error": null
}
```

---

#### REQ-JOB-02 — SSE Log Stream

`GET /api/slice/logs/{job_id}` — Server-Sent Events, one log line per event. Terminal sentinels `__COMPLETED__` and `__FAILED__: <reason>` are unchanged.

---

#### REQ-JOB-03 — Output Download

`GET /api/slice/download/{job_id}` — file download; triggers immediate job eviction after response.

---

#### REQ-JOB-04 — Job TTL

Completed/failed jobs evicted from memory and disk after `JOB_TTL_SECONDS` (default `3600`). A background sweep runs every `JOB_SWEEP_INTERVAL_SECONDS` (default `300`).

---

### ARRANGEMENT

---

#### REQ-ARRANGE-01 — 3MF Arrangement (unchanged)

`POST /api/arrange` — synchronous; accepts a 3MF, runs `--arrange [1] --orient [1]`, returns the rearranged 3MF directly. 35-second timeout.

---

### HEALTH

---

#### REQ-HEALTH-01 — Extended Health Check

`GET /api/health`

```json
{
  "status": "healthy",
  "orcaslicer_installed": true,
  "orcaslicer_version": "2.2.0",
  "config_mounted": true,
  "system_profiles_available": true,
  "catalog_loaded": true,
  "catalog_profile_count": { "machine": 142, "process": 381, "filament": 1204 },
  "active_jobs": 1
}
```

`orcaslicer_version` is cached from a `orcaslicer --version` call at startup. `catalog_loaded` is `false` during the initial catalog build. `active_jobs` helps Themis make load-aware decisions.

---

## Non-Functional Requirements

### REQ-NFR-01 — Catalog Build Time

The catalog must be fully built (all profiles walked and inheritance resolved) within 60 seconds of container start. `GET /api/profiles` returns HTTP 503 with `{"status": "building_catalog"}` until ready. `GET /api/health` reflects `catalog_loaded: false` during this window.

### REQ-NFR-02 — Job Submission Latency

`POST /api/slice/start` must return the job ID within 2 seconds. File save and 3MF preparation happen synchronously before the response (needed to validate UUIDs and compatibility), but OrcaSlicer subprocess launch is async.

### REQ-NFR-03 — Concurrent Jobs

At least 4 concurrent slicing jobs without serialization. Reject with HTTP 503 when `MAX_CONCURRENT_JOBS` (default `4`) are already in `slicing` state.

### REQ-NFR-04 — Profile Cache Consistency

After `POST /api/profiles/upload` returns 200, subsequent `GET /api/profiles` calls must reflect the new profile. The catalog rebuild after upload must complete within 10 seconds.

### REQ-NFR-05 — Error Isolation

A crashing OrcaSlicer process (SIGSEGV, OOM) must not affect other running jobs or the API process itself. Each job runs in its own subprocess with its own working directory.

---

## API Surface Delta

| Endpoint | Status | Change |
|----------|--------|--------|
| `GET /api/profiles` | **Revised** | Add UUIDs, pre-resolved metadata, system profiles, `?machine_uuid=` filter |
| `GET /api/profiles/{uuid}` | **New** | Full profile detail by UUID (all types) |
| `POST /api/slice/start` | **Revised** | Accepts `(manufacturer, model, nozzle)` for machine + UUIDs for process/filament; container handles resolution and 3MF preparation |
| `POST /api/slice/prepared` | **New** | Pre-built 3MF path for migration / advanced use |
| `GET /api/slice/status/{job_id}` | Extended | Add `output_format` field |
| `GET /api/slice/logs/{job_id}` | Keep | Unchanged |
| `GET /api/slice/download/{job_id}` | Extended | Serves correct artifact per `output_format`; triggers eviction |
| `POST /api/arrange` | Keep | Unchanged |
| `POST /api/profiles/upload` | Keep | Triggers catalog rebuild |
| `GET /api/health` | Extended | Add version, catalog status, active_jobs |

Removed from previous design:
- `GET /api/profiles/content` — superseded by `GET /api/profiles/{uuid}` which returns resolved (not raw) data

---

## Configuration Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SLICE_TIMEOUT_SECONDS` | `600` | Max seconds OrcaSlicer subprocess runs before SIGKILL |
| `JOB_TTL_SECONDS` | `3600` | Seconds before completed/failed job is evicted |
| `JOB_SWEEP_INTERVAL_SECONDS` | `300` | Frequency of TTL eviction sweep |
| `MAX_CONCURRENT_JOBS` | `4` | HTTP 503 threshold |
| `SYSTEM_PROFILES_DIR` | `/opt/orcaslicer/resources/profiles` | Root of OrcaSlicer bundled system profiles |
| `CATALOG_NAMESPACE_UUID` | fixed constant | UUID5 namespace for profile UUID derivation — must never change once deployed |

---

## Integration Notes

### Themis Migration Path

**Phase 1 (catalog adoption):** Themis calls `GET /api/profiles` at startup to populate its profile selector UI. Users select profiles; Themis stores machine tuples `(manufacturer, model, nozzle)` and UUIDs (for process and filament) in its `JobPrinterConfig` table instead of profile path strings. Themis continues to prepare 3MFs itself and call `POST /api/slice/prepared`. `PresetResolver` and `ProjectConfigBuilder` stay in Themis for now.

**Phase 2 (full delegation):** Themis removes `PresetResolver` and `ProjectConfigBuilder`. Slice calls switch to `POST /api/slice/start` passing `manufacturer`/`model`/`nozzle` for the machine and UUIDs for process and filament. Themis still handles tool remapping before upload and thumbnail injection after download.

### Machine Identity in Themis's Data Model

Themis's `Printer` model currently stores `orca_printer_profiles` as a list of profile name strings. After migration this becomes a list of `(manufacturer, model, nozzle)` tuples (or a structured object), and `current_orca_printer_profile` becomes `{manufacturer, model, nozzle}`. The machine UUID from the catalog may also be stored as a cache key but the tuple is the stable identifier.

### Filament Identity in Themis's Data Model

Themis's `Printer.loaded_filaments` list currently stores filament profile names as strings. After migration it stores UUIDs. The `GET /api/profiles/{uuid}` endpoint provides the display metadata (type, color, name) needed to render loaded-filament state without embedding profile content in the database.

### What Themis Still Owns After Full Migration

- STL → 3MF geometry wrapping (container can also do this, but Themis may keep it)
- Tool/extruder remapping in `model_settings.config` (printer-specific, stays in Themis)
- Thumbnail extraction from source 3MF and injection into downloaded gcode
- Gcode filename construction (`<stem>_p<plate>_j<job_id>`)
- Per-printer connection, AMS tray mapping, print start signaling
- Job queue, priority, and printer assignment
- Spoolman integration

### OrcaSlicer Profile Inheritance in the Container

The container's internal `PresetResolver` walks the `inherits` field recursively, deep-merging child-over-parent, for all profiles at catalog-build time. The merged result (not the raw leaf JSON) is what the catalog and detail endpoints expose and what gets embedded into `project_settings.config` at slice time. Circular inheritance is detected and logged as a catalog-build warning; such profiles are excluded from the catalog.
