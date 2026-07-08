# OrcaSlicer Headless API

A Dockerized REST API wrapping the [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) CLI for headless 3D model slicing and plate arrangement. Includes a web dashboard at `http://localhost:5000`.

## Quick Start

```bash
docker-compose up -d
```

The API is available at `http://localhost:5000`. Drop your OrcaSlicer preset JSON files into `./config/user/default/{machine,process,filament}/` and they will be picked up automatically.

## Configuration

### `ORCA_VERSION`

The OrcaSlicer AppImage is **not baked into the Docker image**. On first container start, `entrypoint.sh` downloads the AppImage for the version specified by `ORCA_VERSION`, extracts it, and caches it in the `orca-slicer` named volume. Subsequent restarts skip the download as long as the version on disk matches `ORCA_VERSION`.

To upgrade OrcaSlicer without rebuilding the image, set `ORCA_VERSION` to the new version and restart the container. The entrypoint detects the version mismatch, downloads the new release, and replaces the cached install.

```bash
# .env or shell export
ORCA_VERSION=2.4.1   # default
```

## Volumes

| Source | Container path | Purpose |
|--------|---------------|---------|
| `./config` | `/config` | OrcaSlicer preset profiles |
| `./data` | `/data` | Input models and output files |
| `./app` | `/workspace/app` | Application code (live reload in dev) |
| `orca-slicer` (named) | `/opt/orcaslicer` | Cached OrcaSlicer install — persists across restarts |

## API Reference

See [`openapi.yaml`](openapi.yaml) for the full OpenAPI 3.0 spec.

### Slice a model

```bash
# Start job
curl -X POST http://localhost:5000/api/slice/start \
  -F "file=@model.stl" \
  -F 'config={"printer":"default/machine/Creality Ender-3 0.4 nozzle.json","process":"default/process/0.16mm Optimal @Creality Ender3 0.4.json","filaments":{"1":"default/filament/Creality Generic PLA.json"}}'

# Poll status
curl http://localhost:5000/api/slice/status/<job_id>

# Stream logs (SSE)
curl http://localhost:5000/api/slice/logs/<job_id>

# Download result
curl -O http://localhost:5000/api/slice/download/<job_id>
```

### Arrange a 3MF

```bash
curl -X POST http://localhost:5000/api/arrange \
  -F "file=@project.3mf" \
  -F "arrange=true" \
  -F "orient=true" \
  -o arranged_project.3mf
```

### List profiles

```bash
curl http://localhost:5000/api/profiles
```

## Loading Profiles

OrcaSlicer's built-in profiles use inheritance and cannot be used directly. Use `flatten_profiles.py` inside the container to resolve the inheritance chain and write a standalone user preset:

```bash
docker exec orcaslicer-api python3 /workspace/flatten_profiles.py \
  "/opt/orcaslicer/resources/profiles/Creality/machine/Creality Ender-3 0.4 nozzle.json" \
  "/config/user/default/machine/Creality Ender-3 0.4 nozzle.json" \
  "machine"
```

Built-in profiles are located at `/opt/orcaslicer/resources/profiles/` inside the container. The script accepts `machine`, `process`, or `filament` as the third argument.

You can also upload profile JSON files directly via the API:

```bash
curl -X POST http://localhost:5000/api/profiles/upload \
  -F "type=filament" \
  -F "file=@MyFilament.json"
```

## Health Check

```bash
curl http://localhost:5000/api/health
```

## Notes

- Slicing jobs run asynchronously; job state is in-memory and does not survive container restarts.
- The arrange endpoint is synchronous with a 35-second timeout.
- OrcaSlicer requires a virtual display; the container uses `xvfb-run` automatically.
- `shm_size: 1gb` in `docker-compose.yml` is required for OrcaSlicer's renderer.
- First container start requires an internet connection to download the OrcaSlicer AppImage (~300 MB). Subsequent starts use the cached volume and are instant.
