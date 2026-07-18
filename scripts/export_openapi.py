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
