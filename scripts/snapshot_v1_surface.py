"""Regenerate tests/fixtures/api_v1_surface.json (the v1 compatibility baseline).

Only run this when INTENTIONALLY adding to v1 (additions are compatible). Removing or
tightening anything the baseline lists is a breaking change and needs /api/v2
(see docs/api-versioning.md).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_api_v1_compat import surface  # noqa: E402

out = Path(__file__).parent.parent / "tests" / "fixtures" / "api_v1_surface.json"
out.write_text(json.dumps(surface(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"Written {out}")
