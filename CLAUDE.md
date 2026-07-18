# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Communication style
When reporting information, be extremely concise and sacrifice grammar for the sake of concision.


## What this project is

A Dockerized REST API that wraps the OrcaSlicer CLI to perform headless 3D model slicing and plate arrangement. OrcaSlicer is a GUI application run headlessly inside the container via `xvfb-run`. The API is implemented in a single FastAPI file (`app/main.py`) with a single-page HTML frontend (`app/templates/index.html`).

## Commands

**Build and run (Docker):**
```bash
docker-compose build
docker-compose up          # foreground
docker-compose up -d       # background
docker-compose down
```

**Local dev (no Docker — requires OrcaSlicer installed at `/usr/local/bin/orcaslicer`):**
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 5000 --reload
```

**Flatten a system profile into a standalone user profile (run inside the container):**
```bash
docker exec laminus python3 /workspace/flatten_profiles.py \
  "/opt/orcaslicer/resources/profiles/Creality/machine/Creality Ender-3 0.4 nozzle.json" \
  "/config/user/default/machine/Creality Ender-3 0.4 nozzle.json" \
  "machine"
```

**Check API health:**
```bash
curl http://localhost:5000/api/health
```

## Architecture

### Runtime paths (inside container)
- `/config/user/default/{machine,process,filament}/` — user-supplied OrcaSlicer preset JSON files (mounted from `./config` on host)
- `/data/` — general data volume (mounted from `./data` on host)
- `/tmp/jobs/{job_id}/` — per-job working directories created at runtime; not persisted
- `/tmp/arrange/{job_id}/` — temp dirs for arrange operations; cleaned up after response

### Request flow for slicing
1. `POST /api/slice/start` saves the uploaded file to `/tmp/jobs/{uuid}/input/`, creates an in-memory job entry in the global `jobs` dict, and dispatches `run_orcaslicer_task` as a FastAPI `BackgroundTask`.
2. `run_orcaslicer_task` resolves profile paths via the `ProfileCatalog` singleton, builds a `xvfb-run orcaslicer --slice ...` subprocess, and streams stdout line-by-line into a per-job `JobLogger` (an `asyncio.Queue` wrapper).
3. `GET /api/slice/logs/{job_id}` streams those logs as SSE using `StreamingResponse`. The stream terminates when the logger emits `__COMPLETED__` or `__FAILED__:...` sentinel strings.
4. `GET /api/slice/download/{job_id}` returns the first `.gcode` or `.3mf` found in the job's output dir.

### Request flow for arrangement
`POST /api/arrange` runs `xvfb-run orcaslicer --arrange 1 --orient 1 --export-3mf` **synchronously** (35-second timeout) and streams the resulting `.3mf` file back directly, then queues directory cleanup as a background task.

### Profile resolution
`ProfileCatalog` (`app/profile_catalog.py`) scans the OrcaSlicer system profiles dir and `/config/user/` on startup, resolves the `"inherits"` chain into fully merged presets, and builds a name index for fast lookup. Profiles are matched by display `name` or file path when building CLI arguments. The catalog is lazily loaded and cached to `/tmp/laminus_catalog_cache.json`; delete the cache file to force a rebuild. `flatten_profiles.py` (see above) produces standalone user presets from system profiles that can then be placed in `/config/user/`.

### Job state
`jobs` is a plain in-memory dict — all state is lost on container restart. There is no database or persistence layer.

### OrcaSlicer system profiles
OrcaSlicer's built-in profiles use an inheritance chain (`"inherits"` key). They cannot be used directly as user presets. `flatten_profiles.py` recursively resolves inheritance and writes a fully merged, standalone JSON suitable for the `/config/user/` volume. It also patches in required fields (`"from": "user"`, `compatible_printers`, `layer_change_gcode`) that the CLI validator requires.
