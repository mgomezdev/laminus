"""Profile catalog: discovery, inheritance resolution, UUID assignment, caching."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Optional, Tuple

logger = logging.getLogger("orcaslicer-api.catalog")

_CATALOG_NS = uuid.UUID("a7f3c2e1-84b0-4d9e-b1f2-3c8a5d6e7f01")


def _find_file_by_name(name: str, search_roots: list[str]) -> Optional[str]:
    filename = f"{name}.json"
    for root in search_roots:
        for dirpath, _dirs, files in os.walk(root):
            if filename in files:
                return os.path.join(dirpath, filename)
    return None


def resolve_inheritance(
    filepath: str,
    search_roots: list[str],
    _visited: Optional[set[str]] = None,
) -> dict:
    """Return fully merged (flattened) profile dict. Child values override parent."""
    if _visited is None:
        _visited = set()
    real = os.path.realpath(filepath)
    if real in _visited:
        raise ValueError(f"Circular inheritance detected at '{filepath}'")
    _visited.add(real)

    with open(filepath, "r", encoding="utf-8") as fh:
        data: dict = json.load(fh)

    parent_name: Optional[str] = data.get("inherits")
    if parent_name:
        parent_path = _find_file_by_name(parent_name, search_roots)
        if parent_path:
            parent_data = resolve_inheritance(parent_path, search_roots, set(_visited))
            merged = {**parent_data, **data}
        else:
            logger.warning("Parent profile '%s' not found - skipping", parent_name)
            merged = dict(data)
        merged.pop("inherits", None)
        return merged

    result = dict(data)
    result.pop("inherits", None)
    return result
