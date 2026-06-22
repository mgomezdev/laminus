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


def make_profile_uuid(source: str, rel_path: str) -> str:
    """Stable UUID for process/filament profiles derived from source and path."""
    return str(uuid.uuid5(_CATALOG_NS, f"{source}\x00{rel_path}"))


def make_machine_uuid(manufacturer: str, model: str, nozzle: str) -> str:
    """Stable UUID for machine profiles derived from the (manufacturer, model, nozzle) tuple."""
    return str(uuid.uuid5(_CATALOG_NS, f"{manufacturer}|{model}|{nozzle}"))


_MACHINE_NAME_RE = re.compile(
    r"^(?P<mfr>\S.*?)\s+(?P<model>\S+(?:\s+\S+)*?)\s+(?P<nozzle>\d+\.\d+)\s+nozzle\s*$",
    re.IGNORECASE,
)


def parse_machine_name(name: str) -> Optional[Tuple[str, str, str]]:
    """Parse 'Manufacturer Model X.Y nozzle' into (manufacturer, model, nozzle) or None."""
    m = _MACHINE_NAME_RE.match(name.strip())
    if not m:
        return None
    full_prefix = f"{m.group('mfr')} {m.group('model')}"
    nozzle = m.group("nozzle")
    tokens = full_prefix.split()
    mfr_tokens: list[str] = []
    model_tokens: list[str] = []
    for tok in tokens:
        if model_tokens or re.search(r"[\d\-]", tok):
            model_tokens.append(tok)
        else:
            mfr_tokens.append(tok)
    if not mfr_tokens:
        mfr_tokens = [tokens[0]]
        model_tokens = tokens[1:]
    if not model_tokens:
        if len(mfr_tokens) >= 2:
            # All-alpha name: treat first token as manufacturer, rest as model
            model_tokens = mfr_tokens[1:]
            mfr_tokens = mfr_tokens[:1]
        else:
            return None
    return " ".join(mfr_tokens), " ".join(model_tokens), nozzle


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
