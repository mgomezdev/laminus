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
- `/config/user/default/{machine,process,filament}/` — user-supplied OrcaSlicer preset JSON files. Durable volume — this is the whole reason `/config` is mounted.
- `/config/plugins/` — OrcaSlicer plugins (e.g. network plugins), installed once and expected to survive redeploys. Same durable volume as `/config/user/`.
- `/data/jobs.json`, `/data/job_history.json` — job queue/history state. Durable volume; both files are small and worth backing up whole.
- `ORCA_DATADIR` (default `/var/lib/laminus/orca-scratch`, set by `entrypoint.sh`) — the `--datadir` actually handed to the OrcaSlicer CLI. **Not mounted.** Orca writes its own `cache/` and `log/` here as a side effect of running — `cache/` is pure rebuildable scratch and `log/` grows without bound, neither belongs in a backed-up volume. `entrypoint.sh` symlinks `$ORCA_DATADIR/user` → `/config/user` and `$ORCA_DATADIR/plugins` → `/config/plugins`, so Orca sees a normal-looking datadir while the durable `/config` volume only ever contains the two subtrees worth keeping. Falls back to `CONFIG_DIR` (`/config`) when unset, for local non-Docker dev.
- `/tmp/jobs/{job_id}/` — per-job working directories created at runtime; not persisted
- `/tmp/arrange/{job_id}/` — temp dirs for arrange operations; cleaned up after response
- `/tmp/laminus_catalog_cache.json` — rebuildable catalog cache (see Profile resolution below); deliberately not on a durable volume
- `/opt/orcaslicer/` — extracted OrcaSlicer AppImage, on its own cache volume (`laminus-slicer`); wiped and re-extracted when `ORCA_VERSION` changes, not backed up

### Request flow for slicing
1. `POST /api/slice/start` saves the uploaded file to `/tmp/jobs/{uuid}/input/`, creates an in-memory job entry in the global `jobs` dict, and dispatches `run_orcaslicer_task` as a FastAPI `BackgroundTask`.
2. `run_orcaslicer_task` resolves profile paths via the `ProfileCatalog` singleton, builds a `xvfb-run orcaslicer --slice ...` subprocess, and streams stdout line-by-line into a per-job `JobLogger` (an `asyncio.Queue` wrapper).
3. `GET /api/slice/logs/{job_id}` streams those logs as SSE using `StreamingResponse`. The stream terminates when the logger emits `__COMPLETED__` or `__FAILED__:...` sentinel strings.
4. `GET /api/slice/download/{job_id}` returns the first `.gcode` or `.3mf` found in the job's output dir.

### Request flow for arrangement
`POST /api/arrange` runs `xvfb-run orcaslicer --arrange 1 --orient 1 --export-3mf` **synchronously** (35-second timeout) and streams the resulting `.3mf` file back directly, then queues directory cleanup as a background task.

### Profile resolution
`ProfileCatalog` (`app/profile_catalog.py`) walks the OrcaSlicer system profiles dir and `/config/user/` in the background, verifies every `"inherits"` chain resolves, and builds the display catalog (`GET /api/profiles`) plus a name index. It does **not** retain the fully-merged preset per entry — that used to be 40% of an 87 MB cache file. `ProfileCatalog.resolved(uuid)` materializes the merged dict lazily, on demand, memoized per-process (`_RESOLVED_MEMO_MAX = 256`), and is what `build_project_settings` is fed at slice time.

Because resolution happens against the profile tree *as it currently is*, a thin user profile (`"inherits": "<vendor name>"` + only the keys it changes) automatically picks up vendor updates on the next catalog build — no reflattening step needed. This is why `flatten_profiles.py` should be a last resort, not the default path (see below).

The catalog is cached to `/tmp/laminus_catalog_cache.json` (deliberately not the durable `/data` volume — it's fully rebuildable), keyed by `ORCA_VERSION` + a signature of the top-level vendor bundle files (`<Vendor>.json`: name, version, mtime, size — cheap, and catches a bind-mounted OrcaSlicer install self-updating a vendor bundle independently of `ORCA_VERSION`) + every file under `/config/user/`. Any preset change on either side voids the cache and forces re-verification. `POST /api/profiles/rescan` forces a rebuild manually (bypassing the cache check), e.g. after editing profiles directly on a bind mount. `GET /api/profiles/broken` lists profiles dropped from the catalog because their `inherits` chain didn't resolve — the signal an OrcaSlicer update renamed or removed a vendor base a user profile depended on.

### Job state
`jobs` is an in-memory dict, but it's write-through: `_save_jobs()` snapshots serialisable job metadata to `/data/jobs.json` on every change, and `_load_jobs_on_startup()` restores it on boot — any job that was `pending`/`slicing` when the process stopped is marked `failed` on reload (its disk files are gone, so it comes back as a status-only stub). `job_history` is a separate, longer-lived `OrderedDict` backed by `/data/job_history.json`, capped at `JOB_HISTORY_LIMIT` (default 200); it survives a job's eviction from `jobs` (by download or the TTL sweep) so `GET /api/jobs` can still show what happened. Neither file is a database — both are just JSON dumps on the durable `/data` volume.

### API reference for agents
`docs/laminus-api-for-agents.md` documents the full request/response shape for every endpoint (canonical slice workflow, UUID stability, error shapes) — read it before calling the API rather than inferring the contract from this file or from `openapi.json` directly.

### OrcaSlicer system profiles
Prefer a thin user preset: `"inherits": "<vendor profile name>"` plus only the keys you're overriding. laminus resolves the chain itself at catalog-build and slice time, so this rolls forward with vendor updates automatically.

`flatten_profiles.py` recursively resolves inheritance and writes a fully merged, standalone JSON with no `inherits` — use it only for a permanent fork (the vendor profile is being removed, or you need to diverge for good). A flattened profile is frozen at the OrcaSlicer version it was flattened from and will not receive any later vendor fix. It also patches in required fields (`"from": "user"`, `compatible_printers`, `layer_change_gcode`) that the CLI validator requires.
