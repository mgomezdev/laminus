"""Select the HTTP API exercised by the shared test suites."""

import os


API_VERSION = os.environ.get("LAMINUS_TEST_API_VERSION", os.environ.get("LAMINUS_API_VERSION", "v1"))
if API_VERSION not in {"v1", "v2"}:
    raise ValueError(f"Unsupported test API version: {API_VERSION}")

API_PREFIX = f"/api/{API_VERSION}"
