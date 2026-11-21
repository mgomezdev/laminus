"""Export the Laminus OpenAPI spec to openapi.json at the repo root.

Run from the repo root:
    python scripts/export_openapi.py

CI uses this + `git diff --exit-code openapi.json` to enforce spec freshness.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.main import app  # noqa: E402

spec = app.openapi()
out = Path(__file__).parent.parent / "openapi.json"
out.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
print(f"Written {out}")

# v1 slice of the same spec: the stable, versioned contract (docs/api-versioning.md).
v1 = dict(spec)
v1["paths"] = {p: v for p, v in spec["paths"].items() if p == "/api/v1" or p.startswith("/api/v1/")}
v1["info"] = {**spec["info"], "title": f"{spec['info']['title']} (API v1)"}
out_v1 = Path(__file__).parent.parent / "openapi-v1.json"
out_v1.write_text(json.dumps(v1, indent=2) + "\n", encoding="utf-8")
print(f"Written {out_v1}")
