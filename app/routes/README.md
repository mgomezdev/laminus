# app/routes

Route handlers, one module per area. `app/main.py` keeps the FastAPI `app`, configuration, process state (`jobs`,
`job_history`, `catalog`), the lifespan, job persistence and the helpers every area shares.

| Module | Routes | Also holds |
|---|---|---|
| `profiles.py` | `/profiles/merged-config`, `/profiles/broken`, the neutral `/profiles/machines/*` and `/profiles/models/*` lookups, `/profiles/upload`, `/profiles/rescan` | request models |
| `slicing.py` | `/slice/prepared`, `/slice/thumbnail` | `run_orcaslicer_task` (the job's subprocess), `start_slice` and `download_sliced_file` (called by `api_v1.py`) |
| `projects.py` | `/arrange`, `/projects/inspect`, `/projects/previews`, `/projects/subset`, `/projects/merge`, `/projects/measure` | `_orca_arrange`, `_embed_manifest`, retarget helpers |
| `packing.py` | `/pack`, `/pack/next`, `/pack/mixed` | pack item parsing, mixed-selection project building |
| `system.py` | `/` (dashboard), `/health`, `/cleanup`, `/test/known-profile` | stale temp scan |

`/api/v1/jobs*` and `/api/v1/profiles` (list / get) are in `app/api_v1.py`.

## The one rule

Anything that lives in `app/main.py`, and any function from **another** route module, is read as `main.<name>`:

```python
import app.main as main

@main.app.post("/api/v1/example")
async def example():
    if main.catalog is None:              # state: always the live value
        return main._catalog_unavailable()   # shared helper
    await main._orca_arrange(...)         # a function from routes/projects.py, re-exported by main
```

Why: `main.py` imports these modules at its bottom and re-exports their names, so `app.main.<name>` is the single place a
value is looked up. State that is reassigned (`catalog`) is never stale, and a test that does
`monkeypatch.setattr(app.main, "catalog", ...)` or patches a helper there takes effect in every handler. A bare
`from app.main import catalog` would freeze the value at import time and break both.

Use a bare name only for something defined in the same module **and** needed at import time (a decorator argument, a type
annotation, a default). Pure helpers from `app/*.py` (`subset_3mf`, `manifest`, ...) are imported directly.

## Adding a route

1. Put it in the module for its area, decorated with `@main.app.<method>(...)`.
2. Add its name to that module's re-export list at the bottom of `app/main.py`.
3. `python scripts/export_openapi.py` and commit the specs; add it to both halves of `tests/test_mock_contract.py` if the
   mock should serve it.

A new module is imported from the bottom of `app/main.py`; it may use names from modules imported before it at import
time, and its routes match after theirs.
