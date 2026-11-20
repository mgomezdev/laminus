# OrcaSlicer Headless API

A Dockerized REST API wrapping the [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) CLI for headless 3D model slicing and plate arrangement. Includes a web dashboard at `http://localhost:5000` (slice form, job history with status filter, live slicer-slot queue, and per-job log viewer deep-linkable as `/#job=<id>`). No auth; the port binds on all interfaces, so it's reachable from the LAN or a Tailscale tailnet at `http://<host>:5000` — don't expose it to the internet.

## Quick Start

```bash
docker compose up --build -d
```

The API and dashboard are available at `http://localhost:5000`. New integrations use `/api/v2`. Drop preset JSON files into `./config/v2/user/default/{machine,process,filament}/`. Bridge tests can use `/api/v1` and `./config/v1/user/default/`.

## Configuration

### OrcaSlicer releases

The container downloads and caches both OrcaSlicer AppImages on first start. Deprecated `/api/v1` is pinned to 2.4.2. `/api/v2` defaults to 2.5.1 and accepts another 2.5.x release through `ORCA_V2_VERSION`. Recreate the container after changing that setting. Each API version owns its own presets, jobs, and artifacts.

```bash
# .env or shell export
ORCA_V2_VERSION=2.5.1
```

### Orca desktop and plugins

Use the opt-in `orca-desktop` service for the Orca 2.5 plugin manager. It defaults to OrcaSlicer 2.5.1 and keeps its own AppImage cache and desktop settings. Export presets created by desktop plugins into the target API version’s preset directory.

```bash
# Build the desktop image and set a VNC password when prompted.
docker compose build orca-desktop
mkdir -p config/orca-desktop
docker compose run --rm --no-deps --entrypoint x11vnc orca-desktop \
  -storepasswd /config/orca-desktop/vnc.pass

# Start OrcaSlicer and connect a VNC client to 127.0.0.1:5900.
ORCA_DESKTOP_VNC=1 docker compose up -d orca-desktop
```

The VNC port is published only on the Docker host's loopback interface. For a remote Docker host, forward the port over SSH. The desktop can also run without VNC by leaving `ORCA_DESKTOP_VNC` at its default of `0`.

- Install and update plugins through Orca's **Plugins** dialog. Use its settings and permission prompts to configure each plugin.
- To seed trusted local `.py` or `.whl` packages on startup, put them in `config/orca-desktop/packages/`. The filename identifies the install. Startup installs new packages and preserves existing installs, enabled flags and permissions. Use Orca's plugin manager to replace a package. Remove a package from the startup directory before uninstalling it in Orca.
- The desktop keeps installed packages, plugin settings and native logs in `config/orca-desktop/state/`. Set `ORCA_DESKTOP_CONFIG_DIR` to move the desktop's bind mount and `ORCA_DESKTOP_VNC_PORT` to choose a different host port.
- If a plugin requires Orca to restart, run `docker compose restart orca-desktop`. To switch the desktop release, set `ORCA_DESKTOP_VERSION` and run `docker compose up -d orca-desktop` again with your VNC setting.

```bash
docker compose exec orca-desktop orca-diagnostics plugins
docker compose exec orca-desktop orca-diagnostics logs --match spoolman
```

The inventory shows which packages are installed and enabled. Use the native logs to confirm which plugins loaded successfully or to investigate load failures. Spoolman runs from inside the desktop container, so configure it with an endpoint the container can reach.

## Volumes

| Source | Container path | Purpose |
|--------|---------------|---------|
| `./config` | `/config` | OrcaSlicer presets for each API version |
| `./data` | `/data` | Job state for each API version |
| `./app` | `/workspace/app` | Application code (live reload in dev) |
| `laminus-slicer` (named) | `/opt/orcaslicer` | Cached v1 and v2 engines and matching JSON presets |
| `./config/orca-desktop` | `/config/orca-desktop` in `orca-desktop` | Local plugin packages, desktop settings and native logs |
| `laminus-orca-desktop-slicer` (named) | `/opt/orcaslicer` in `orca-desktop` | Cached desktop OrcaSlicer install |

## API Reference

New integrations should use `/api/v2` and [`openapi-v2.json`](openapi-v2.json). Use [`openapi-v1.json`](openapi-v1.json) for bridge testing with the deprecated API. [Versioning policy](docs/api-versioning.md)

See [`openapi.json`](openapi.json) for the full OpenAPI 3.0 spec (CI-regenerated, so it always matches the running code), or [`docs/laminus-api-for-agents.md`](docs/laminus-api-for-agents.md) for a walked-through guide to the full slice workflow, including profile discovery and UUID stability.

### Slice a model

Profiles are referenced by UUID, resolved via `GET /api/v2/profiles` (or `machine_uuid` directly if you already have it — see the agent doc above):

```bash
# Start job (202; response.id is the job ID)
curl -X POST http://localhost:5000/api/v2/jobs \
  -F "file=@model.stl" \
  -F "machine_uuid=<uuid-from-catalog>" \
  -F "process_uuid=<uuid-from-catalog>" \
  -F 'filament_uuids=["<uuid-from-catalog>"]' \
  -F "plate=1"

# Poll status
curl http://localhost:5000/api/v2/jobs/<job_id>

# Stream logs (SSE)
curl http://localhost:5000/api/v2/jobs/<job_id>/events

# Download result
curl -O http://localhost:5000/api/v2/jobs/<job_id>/artifact
```

### Arrange a 3MF

```bash
curl -X POST http://localhost:5000/api/v2/arrange \
  -F "file=@project.3mf" \
  -F "arrange=true" \
  -F "orient=true" \
  -o arranged_project.3mf
```

### List profiles

```bash
curl http://localhost:5000/api/v2/profiles
```

## Loading Profiles

Prefer a thin user preset: drop a JSON file into `./config/v2/user/default/{machine,process,filament}/` with `"inherits": "<vendor profile name>"` plus only the keys you're overriding. Laminus resolves the inheritance chain itself at catalog-build and slice time, so this rolls forward with vendor OrcaSlicer updates automatically — no reflattening needed.

Use `flatten_profiles.py` only as a last resort, for a permanent fork (the vendor profile is being removed, or you need to diverge for good). A flattened profile is frozen at the OrcaSlicer version it was flattened from and won't pick up later vendor fixes:

```bash
docker exec laminus python3 /workspace/flatten_profiles.py \
  "/opt/orcaslicer/v2/json-profiles/Creality/machine/Creality Ender-3 0.4 nozzle.json" \
  "/config/v2/user/default/machine/Creality Ender-3 0.4 nozzle.json" \
  "machine"
```

Built-in profiles are located at `/opt/orcaslicer/v2/json-profiles/` inside the container. The script accepts `machine`, `process`, or `filament` as the third argument.

You can also upload profile JSON files directly via the API:

```bash
curl -X POST http://localhost:5000/api/v2/profiles/upload \
  -F "type=filament" \
  -F "file=@MyFilament.json"
```

## Health Check

```bash
curl http://localhost:5000/api/v2/health
```

## Notes

- Slicing jobs run asynchronously; job status/history is persisted to the `/data` volume, but any job still `pending`/`slicing` when the container stops comes back as `failed` on restart (its temp files are gone).
- The arrange endpoint is synchronous with a 35-second timeout.
- OrcaSlicer requires a virtual display; the container uses `xvfb-run` automatically.
- `shm_size: 1gb` in `docker-compose.yml` is required for OrcaSlicer's renderer.
- First container start requires an internet connection to download the OrcaSlicer AppImage (~300 MB). Subsequent starts use the cached volume and are instant.
