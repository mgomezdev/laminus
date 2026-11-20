"""API identity for one Laminus worker."""

import os
import re


API_VERSION = os.environ.get("LAMINUS_API_VERSION", "v1")
if API_VERSION not in {"v1", "v2"}:
    raise ValueError(f"Unsupported LAMINUS_API_VERSION: {API_VERSION}")

API_PREFIX = f"/api/{API_VERSION}"
V1_DEPRECATED_AT = "@1795132800"  # 2026-11-20 UTC (RFC 9745)


def api_docs(text):
    """Render references in shared API descriptions for the active version."""
    return re.sub(r"/api/v1(?=/|\b)", API_PREFIX, text)
