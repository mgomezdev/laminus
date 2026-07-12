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


def _build_name_index(roots: list[str]) -> dict[str, str]:
    """Single-pass index of all JSON profile names → absolute paths (first root wins)."""
    index: dict[str, str] = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for filename in files:
                if filename.endswith(".json") and not filename.startswith("."):
                    stem = os.path.splitext(filename)[0]
                    if stem not in index:
                        index[stem] = os.path.join(dirpath, filename)
    return index


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
# Snapmaker uses "Brand Model (X.Y nozzle)" with parentheses
_MACHINE_NAME_PAREN_RE = re.compile(
    r"^(?P<mfr>\S.*?)\s+(?P<model>\S+(?:\s+\S+)*?)\s+\((?P<nozzle>\d+\.\d+)\s+nozzle\)\s*$",
    re.IGNORECASE,
)


def parse_machine_name(name: str) -> Optional[Tuple[str, str, str]]:
    """Parse 'Manufacturer Model X.Y nozzle' (or parenthesised variant) into (manufacturer, model, nozzle) or None."""
    m = _MACHINE_NAME_RE.match(name.strip()) or _MACHINE_NAME_PAREN_RE.match(name.strip())
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
    _name_index: Optional[dict[str, str]] = None,
    _resolved_cache: Optional[dict[str, dict]] = None,
) -> dict:
    """Return fully merged (flattened) profile dict. Child values override parent."""
    if _visited is None:
        _visited = set()
    real = os.path.realpath(filepath)
    if _resolved_cache is not None and real in _resolved_cache:
        return _resolved_cache[real]
    if real in _visited:
        raise ValueError(f"Circular inheritance detected at '{filepath}'")
    _visited.add(real)

    with open(filepath, "r", encoding="utf-8") as fh:
        data: dict = json.load(fh)

    parent_name: Optional[str] = data.get("inherits")
    if parent_name:
        parent_path = (
            _name_index.get(parent_name) if _name_index is not None
            else _find_file_by_name(parent_name, search_roots)
        )
        if parent_path:
            parent_data = resolve_inheritance(
                parent_path, search_roots, set(_visited), _name_index, _resolved_cache,
            )
            merged = {**parent_data, **data}
        else:
            logger.warning("Parent profile '%s' not found - skipping", parent_name)
            merged = dict(data)
        merged.pop("inherits", None)
        result = merged
    else:
        result = dict(data)
        result.pop("inherits", None)

    if _resolved_cache is not None:
        _resolved_cache[real] = result
    return result


SYSTEM_PROFILE_TYPES = ("machine", "process", "filament")

_STRIP_META = {"inherits", "compatible_printers", "compatible_printers_condition",
               "is_custom_defined", "from", "instantiation"}


def _display_name(name: str) -> str:
    at = name.find(" @")
    return name[:at].strip() if at != -1 else name.strip()


class ProfileCatalog:
    """Scanned, inheritance-resolved, UUID-annotated profile catalog."""

    def __init__(self, system_dir: str, user_dir: str):
        self._system_dir = system_dir
        self._user_dir = user_dir
        self._by_uuid: dict[str, dict] = {}
        self._catalog: dict[str, list[dict]] = {"machine": [], "process": [], "filament": []}
        self._built = False
        self._dict_cache: dict | None = None

    def build(self) -> None:
        catalog: dict[str, list[dict]] = {"machine": [], "process": [], "filament": []}
        by_uuid: dict[str, dict] = {}
        search_roots = [self._system_dir, self._user_dir]

        # Pre-build name→path index and resolved-profile cache so parent lookups
        # are O(1) and shared base profiles are loaded only once (critical for
        # large profile directories mounted via Docker volume on Windows/WSL2).
        name_index = _build_name_index(search_roots)
        resolved_cache: dict[str, dict] = {}

        for source, root in [("system", self._system_dir), ("user", self._user_dir)]:
            # system first, user second — user entries overwrite system entries by UUID
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                # OrcaSlicer vendors nest machine/process/filament profiles in
                # subdirectories (e.g. Elegoo/machine/ECC/).  Find the nearest
                # ancestor whose name matches a profile type rather than only
                # looking at the immediate parent directory.
                path_parts = os.path.relpath(dirpath, root).replace("\\", "/").split("/")
                ptype = next((p for p in path_parts if p in SYSTEM_PROFILE_TYPES), None)
                if ptype is None:
                    continue
                for filename in files:
                    if not filename.endswith(".json") or filename.startswith("."):
                        continue
                    filepath = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(filepath, root).replace("\\", "/")
                    try:
                        resolved = resolve_inheritance(
                            filepath, search_roots,
                            _name_index=name_index, _resolved_cache=resolved_cache,
                        )
                    except Exception as exc:
                        logger.warning("Skipping '%s': %s", filepath, exc)
                        continue
                    entry = self._make_entry(resolved, ptype, source, rel_path)
                    catalog[ptype].append(entry)
                    by_uuid[entry["uuid"]] = entry

        self._catalog = catalog
        self._by_uuid = by_uuid
        self._dict_cache = None  # invalidate on rebuild
        self._built = True
        logger.info("Catalog built: %s", {k: len(v) for k, v in catalog.items()})

    def _make_entry(self, resolved: dict, ptype: str, source: str, rel_path: str) -> dict:
        name: str = resolved.get("name", os.path.splitext(os.path.basename(rel_path))[0])

        if ptype == "machine":
            parsed = parse_machine_name(name)
            manufacturer, model, nozzle = parsed if parsed else (None, None, None)
            entry_uuid = (
                make_machine_uuid(manufacturer, model, nozzle)
                if parsed else make_profile_uuid(source, rel_path)
            )
            return {
                "uuid": entry_uuid, "type": "machine", "name": name, "source": source,
                "rel_path": rel_path, "manufacturer": manufacturer, "model": model, "nozzle": nozzle,
                "nozzle_diameter": resolved.get("nozzle_diameter"),
                "bed_size_x": resolved.get("bed_size_x"), "bed_size_y": resolved.get("bed_size_y"),
                "extruder_count": resolved.get("extruder_count", 1),
                "_resolved": resolved,
            }

        if ptype == "filament":
            colour_raw = resolved.get("filament_colour", "#FFFFFF")
            colour = colour_raw[0] if isinstance(colour_raw, list) else colour_raw
            ft_raw = resolved.get("filament_type", "")
            filament_type = ft_raw[0] if isinstance(ft_raw, list) else ft_raw
            fd_raw = resolved.get("filament_diameter", 1.75)
            filament_diameter = fd_raw[0] if isinstance(fd_raw, list) else fd_raw
            fv_raw = resolved.get("filament_vendor", "")
            filament_vendor = fv_raw[0] if isinstance(fv_raw, list) else fv_raw
            return {
                "uuid": make_profile_uuid(source, rel_path), "type": "filament",
                "name": name, "display_name": _display_name(name), "source": source,
                "rel_path": rel_path,
                "filament_type": filament_type,
                "filament_colour": colour,
                "filament_vendor": filament_vendor,
                "filament_diameter": filament_diameter,
                "filament_density": resolved.get("filament_density"),
                "nozzle_temperature": resolved.get("nozzle_temperature"),
                "nozzle_temperature_range_low": resolved.get("nozzle_temperature_range_low"),
                "nozzle_temperature_range_high": resolved.get("nozzle_temperature_range_high"),
                "bed_temperature": resolved.get("bed_temperature"),
                "bed_temperature_initial_layer": resolved.get("bed_temperature_initial_layer"),
                "compatible_printers": resolved.get("compatible_printers", []),
                "_resolved": resolved,
            }

        return {
            "uuid": make_profile_uuid(source, rel_path), "type": "process",
            "name": name, "source": source, "rel_path": rel_path,
            "layer_height": resolved.get("layer_height"),
            "speed": resolved.get("speed"),
            "compatible_printers": resolved.get("compatible_printers", []),
            "_resolved": resolved,
        }

    def get_by_uuid(self, uid: str) -> Optional[dict]:
        return self._by_uuid.get(uid)

    def get_machine(self, manufacturer: str, model: str, nozzle: str) -> Optional[dict]:
        entry = self._by_uuid.get(make_machine_uuid(manufacturer, model, nozzle))
        if entry:
            return entry
        for m in self._catalog["machine"]:
            if m.get("manufacturer") == manufacturer and m.get("model") == model and m.get("nozzle") == nozzle:
                return m
        return None

    def as_dict(
        self,
        manufacturer: Optional[str] = None,
        model: Optional[str] = None,
        nozzle: Optional[str] = None,
    ) -> dict:
        if manufacturer or model or nozzle:
            # filtered view — not cached
            result = {ptype: [self._public(e) for e in entries] for ptype, entries in self._catalog.items()}
        elif self._dict_cache is not None:
            return self._dict_cache
        else:
            result = {ptype: [self._public(e) for e in entries] for ptype, entries in self._catalog.items()}
            self._dict_cache = result
        if manufacturer and model and nozzle:
            machine_entry = self.get_machine(manufacturer, model, nozzle)
            machine_name = machine_entry["name"] if machine_entry else None
            for ptype in ("process", "filament"):
                result[ptype] = [
                    e for e in result[ptype]
                    if not e.get("compatible_printers")
                    or (machine_name and machine_name in e["compatible_printers"])
                ]
        return result

    @property
    def is_built(self) -> bool:
        return self._built

    @property
    def counts(self) -> dict:
        return {k: len(v) for k, v in self._catalog.items()}

    def save_to_cache(self, path: str, cache_key: str) -> None:
        """Write catalog state to a JSON file keyed by cache_key."""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cache_key": cache_key, "catalog": self._catalog, "by_uuid": self._by_uuid}, f)
        os.replace(tmp, path)

    @classmethod
    def load_from_cache(cls, path: str, expected_key: str, system_dir: str, user_dir: str) -> "ProfileCatalog | None":
        """Return a pre-built ProfileCatalog from cache, or None if stale/missing."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if data.get("cache_key") != expected_key:
            return None
        cat = cls(system_dir=system_dir, user_dir=user_dir)
        cat._catalog = data["catalog"]
        cat._by_uuid = data["by_uuid"]
        cat._built = True
        return cat

    @staticmethod
    def _public(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if not k.startswith("_")}
