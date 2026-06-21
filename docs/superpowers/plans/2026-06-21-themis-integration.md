# Themis Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the profile catalog (with inheritance resolution, stable UUIDs, machine manufacturer/model/nozzle tuples) and UUID-based slice endpoint so the orca container can replace Themis's direct OrcaSlicer calls.

**Architecture:** Three new modules (`profile_catalog.py`, `project_config_builder.py`, `stl_to_3mf.py`) support a revised `main.py`. The catalog is built once at startup by walking system + user profile directories and resolving the full `inherits` chain for every profile. The revised slice endpoint accepts a machine tuple + process/filament UUIDs, resolves them through the catalog, builds `project_settings.config`, embeds it into the submitted 3MF, then runs OrcaSlicer.

**Tech Stack:** Python 3.11, FastAPI, asyncio, zipfile (stdlib), uuid (stdlib), json (stdlib), struct (stdlib for STL binary parse), pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `app/profile_catalog.py` | Create | Inheritance resolver, UUID gen, catalog build and cache |
| `app/project_config_builder.py` | Create | Merge flattened machine+process+filament presets into project_settings dict; embed into 3MF ZIP |
| `app/stl_to_3mf.py` | Create | Wrap bare STL (binary or ASCII) into a minimal valid 3MF |
| `app/main.py` | Modify | Wire catalog into lifespan; revise endpoints per spec |
| `tests/conftest.py` | Create | Shared fixtures: tmp profile dirs with sample JSONs |
| `tests/test_profile_catalog.py` | Create | Unit tests for resolver, UUID stability, catalog filtering |
| `tests/test_project_config_builder.py` | Create | Unit tests for merge logic and 3MF embed |
| `tests/test_stl_to_3mf.py` | Create | Unit tests for STL parse and 3MF structure |

---

## Task 1: ProfileCatalog - Inheritance Resolver

**Files:**
- Create: `app/profile_catalog.py`
- Create: `tests/test_profile_catalog.py`

- [ ] **Step 1.1: Write failing test for inheritance resolver**

```python
# tests/test_profile_catalog.py
import json, os, pytest
from app.profile_catalog import resolve_inheritance

def test_resolve_flat_profile(tmp_path):
    p = tmp_path / "leaf.json"
    p.write_text(json.dumps({"name": "Leaf", "layer_height": 0.2}))
    result = resolve_inheritance(str(p), search_roots=[str(tmp_path)])
    assert result["layer_height"] == 0.2
    assert "inherits" not in result

def test_resolve_single_parent(tmp_path):
    parent = tmp_path / "Parent.json"
    parent.write_text(json.dumps({"name": "Parent", "layer_height": 0.3, "speed": 50}))
    child = tmp_path / "Child.json"
    child.write_text(json.dumps({"name": "Child", "inherits": "Parent", "layer_height": 0.2}))
    result = resolve_inheritance(str(child), search_roots=[str(tmp_path)])
    assert result["layer_height"] == 0.2
    assert result["speed"] == 50
    assert "inherits" not in result

def test_resolve_cycle_raises(tmp_path):
    a = tmp_path / "A.json"
    b = tmp_path / "B.json"
    a.write_text(json.dumps({"name": "A", "inherits": "B"}))
    b.write_text(json.dumps({"name": "B", "inherits": "A"}))
    with pytest.raises(ValueError, match="[Cc]ircular"):
        resolve_inheritance(str(a), search_roots=[str(tmp_path)])

def test_resolve_missing_parent_returns_child(tmp_path):
    child = tmp_path / "Child.json"
    child.write_text(json.dumps({"name": "Child", "inherits": "Ghost", "layer_height": 0.2}))
    result = resolve_inheritance(str(child), search_roots=[str(tmp_path)])
    assert result["layer_height"] == 0.2
```

- [ ] **Step 1.2: Run tests - expect ImportError/FAIL**

```bash
python -m pytest tests/test_profile_catalog.py::test_resolve_flat_profile -v 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'resolve_inheritance'`

- [ ] **Step 1.3: Implement resolve_inheritance in app/profile_catalog.py**

```python
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
            parent_data = resolve_inheritance(parent_path, search_roots, _visited)
            merged = {**parent_data, **data}
        else:
            logger.warning("Parent profile '%s' not found - skipping", parent_name)
            merged = dict(data)
        merged.pop("inherits", None)
        return merged

    result = dict(data)
    result.pop("inherits", None)
    return result
```

- [ ] **Step 1.4: Run tests - expect PASS**

```bash
python -m pytest tests/test_profile_catalog.py -v 2>&1 | head -20
```

Expected: 4 PASSED

- [ ] **Step 1.5: Commit**

```bash
git add app/profile_catalog.py tests/test_profile_catalog.py
git commit -m "feat: add profile inheritance resolver"
```

---

## Task 2: ProfileCatalog - UUID Generation and Machine Tuple Parsing

**Files:**
- Modify: `app/profile_catalog.py`
- Modify: `tests/test_profile_catalog.py`

- [ ] **Step 2.1: Write failing tests**

```python
# append to tests/test_profile_catalog.py
from app.profile_catalog import make_profile_uuid, make_machine_uuid, parse_machine_name

def test_make_profile_uuid_is_stable():
    u1 = make_profile_uuid("system", "Bambu Lab/filament/Bambu PLA Basic.json")
    u2 = make_profile_uuid("system", "Bambu Lab/filament/Bambu PLA Basic.json")
    assert u1 == u2

def test_make_profile_uuid_differs_by_source():
    assert make_profile_uuid("system", "foo/bar.json") != make_profile_uuid("user", "foo/bar.json")

def test_make_machine_uuid_is_stable():
    assert make_machine_uuid("Bambu Lab", "P1S", "0.4") == make_machine_uuid("Bambu Lab", "P1S", "0.4")

def test_make_machine_uuid_differs_by_nozzle():
    assert make_machine_uuid("Bambu Lab", "P1S", "0.4") != make_machine_uuid("Bambu Lab", "P1S", "0.6")

def test_parse_machine_name_standard():
    mfr, model, nozzle = parse_machine_name("Bambu Lab P1S 0.4 nozzle")
    assert mfr == "Bambu Lab"
    assert model == "P1S"
    assert nozzle == "0.4"

def test_parse_machine_name_multi_word_model():
    mfr, model, nozzle = parse_machine_name("Creality Ender-3 V2 0.4 nozzle")
    assert mfr == "Creality"
    assert model == "Ender-3 V2"
    assert nozzle == "0.4"

def test_parse_machine_name_no_match_returns_none():
    assert parse_machine_name("Custom Handbuilt Printer") is None
```

- [ ] **Step 2.2: Run tests - expect ImportError/FAIL**

```bash
python -m pytest tests/test_profile_catalog.py -k "uuid or machine" -v 2>&1 | head -20
```

- [ ] **Step 2.3: Add UUID helpers and machine name parser to app/profile_catalog.py**

Add after `_find_file_by_name` (before `resolve_inheritance`):

```python
def make_profile_uuid(source: str, rel_path: str) -> str:
    """Stable UUID for process/filament profiles derived from source and path."""
    return str(uuid.uuid5(_CATALOG_NS, f"{source}:{rel_path}"))


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
        return None
    return " ".join(mfr_tokens), " ".join(model_tokens), nozzle
```

- [ ] **Step 2.4: Run all catalog tests - expect PASS**

```bash
python -m pytest tests/test_profile_catalog.py -v
```

- [ ] **Step 2.5: Commit**

```bash
git add app/profile_catalog.py tests/test_profile_catalog.py
git commit -m "feat: add profile UUID helpers and machine name parser"
```

---

## Task 3: ProfileCatalog - Full Catalog Build and Lookup

**Files:**
- Modify: `app/profile_catalog.py`
- Create: `tests/conftest.py`
- Modify: `tests/test_profile_catalog.py`

- [ ] **Step 3.1: Create tests/conftest.py with shared fixtures**

```python
# tests/conftest.py
import json, os, pytest


def write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


@pytest.fixture
def profile_tree(tmp_path):
    base = str(tmp_path)
    write_json(
        f"{base}/system/Bambu Lab/machine/Bambu Lab P1S 0.4 nozzle.json",
        {"name": "Bambu Lab P1S 0.4 nozzle", "bed_size_x": 256, "bed_size_y": 256, "extruder_count": 1, "nozzle_diameter": [0.4]},
    )
    write_json(
        f"{base}/system/Bambu Lab/process/FFF Settings.json",
        {"name": "FFF Settings", "layer_height": 0.3, "speed": 50},
    )
    write_json(
        f"{base}/system/Bambu Lab/process/0.20mm Standard @BBL X1E.json",
        {"name": "0.20mm Standard @BBL X1E", "inherits": "FFF Settings", "layer_height": 0.2, "compatible_printers": ["Bambu Lab P1S 0.4 nozzle"]},
    )
    write_json(
        f"{base}/system/Bambu Lab/filament/Bambu PLA Basic @BBL X1E 0.4 nozzle.json",
        {"name": "Bambu PLA Basic @BBL X1E 0.4 nozzle", "filament_type": "PLA", "filament_colour": "#FFFFFF", "filament_vendor": "Bambu Lab", "filament_diameter": 1.75, "filament_density": 1.24, "nozzle_temperature": 220, "nozzle_temperature_range_low": 190, "nozzle_temperature_range_high": 240, "bed_temperature": 35, "bed_temperature_initial_layer": 35, "compatible_printers": ["Bambu Lab P1S 0.4 nozzle"]},
    )
    write_json(
        f"{base}/user/default/machine/My Custom Printer.json",
        {"name": "My Custom Printer", "bed_size_x": 220, "bed_size_y": 220, "extruder_count": 1},
    )
    return {"system_dir": f"{base}/system", "user_dir": f"{base}/user"}
```

- [ ] **Step 3.2: Write failing catalog tests**

```python
# append to tests/test_profile_catalog.py
from app.profile_catalog import ProfileCatalog

def test_catalog_build_counts(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    data = cat.as_dict()
    assert len(data["machine"]) == 2
    assert len(data["process"]) == 2
    assert len(data["filament"]) == 1

def test_catalog_machine_has_tuple_fields(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    p1s = next(m for m in cat.as_dict()["machine"] if "P1S" in m["name"])
    assert p1s["manufacturer"] == "Bambu Lab"
    assert p1s["model"] == "P1S"
    assert p1s["nozzle"] == "0.4"
    assert "uuid" in p1s

def test_catalog_filament_display_name(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    fil = cat.as_dict()["filament"][0]
    assert fil["display_name"] == "Bambu PLA Basic"
    assert fil["source"] == "system"

def test_catalog_process_inheritance_resolved(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    standard = next(p for p in cat.as_dict()["process"] if "Standard" in p["name"])
    assert standard["layer_height"] == 0.2
    assert standard["speed"] == 50

def test_get_by_uuid(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    fil = cat.as_dict()["filament"][0]
    assert cat.get_by_uuid(fil["uuid"]) is not None

def test_get_machine_by_tuple(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    m = cat.get_machine("Bambu Lab", "P1S", "0.4")
    assert m is not None and m["bed_size_x"] == 256

def test_filter_by_machine_tuple(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    data = cat.as_dict(manufacturer="Bambu Lab", model="P1S", nozzle="0.4")
    for p in data["process"]:
        assert not p.get("compatible_printers") or "Bambu Lab P1S 0.4 nozzle" in p["compatible_printers"]
```

- [ ] **Step 3.3: Run tests - expect FAIL**

```bash
python -m pytest tests/test_profile_catalog.py -k "catalog" -v 2>&1 | head -20
```

- [ ] **Step 3.4: Implement ProfileCatalog class in app/profile_catalog.py**

Append to `app/profile_catalog.py` after `parse_machine_name`:

```python
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

    def build(self) -> None:
        catalog: dict[str, list[dict]] = {"machine": [], "process": [], "filament": []}
        by_uuid: dict[str, dict] = {}
        search_roots = [self._system_dir, self._user_dir]

        for source, root in [("system", self._system_dir), ("user", self._user_dir)]:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                ptype = os.path.basename(dirpath)
                if ptype not in SYSTEM_PROFILE_TYPES:
                    continue
                for filename in files:
                    if not filename.endswith(".json") or filename.startswith("."):
                        continue
                    filepath = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(filepath, root).replace("\\", "/")
                    try:
                        resolved = resolve_inheritance(filepath, search_roots)
                    except Exception as exc:
                        logger.warning("Skipping '%s': %s", filepath, exc)
                        continue
                    entry = self._make_entry(resolved, ptype, source, rel_path)
                    catalog[ptype].append(entry)
                    by_uuid[entry["uuid"]] = entry

        self._catalog = catalog
        self._by_uuid = by_uuid
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
            return {
                "uuid": make_profile_uuid(source, rel_path), "type": "filament",
                "name": name, "display_name": _display_name(name), "source": source,
                "rel_path": rel_path,
                "filament_type": resolved.get("filament_type", ""),
                "filament_colour": colour,
                "filament_vendor": resolved.get("filament_vendor", ""),
                "filament_diameter": resolved.get("filament_diameter", 1.75),
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
        result = {ptype: [self._public(e) for e in entries] for ptype, entries in self._catalog.items()}
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

    @staticmethod
    def _public(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if not k.startswith("_")}
```

- [ ] **Step 3.5: Run all catalog tests - expect PASS**

```bash
python -m pytest tests/test_profile_catalog.py -v
```

- [ ] **Step 3.6: Commit**

```bash
git add app/profile_catalog.py tests/conftest.py tests/test_profile_catalog.py
git commit -m "feat: implement ProfileCatalog with build, lookup, and filtering"
```

---

## Task 4: ProjectConfigBuilder - Preset Merger and 3MF Embedder

**Files:**
- Create: `app/project_config_builder.py`
- Create: `tests/test_project_config_builder.py`

- [ ] **Step 4.1: Write failing tests**

```python
# tests/test_project_config_builder.py
import json, zipfile, pytest
from app.project_config_builder import build_project_settings, embed_project_settings

def _machine():
    return {"name": "Bambu Lab P1S 0.4 nozzle", "extruder_count": 1,
            "bed_size_x": 256, "nozzle_diameter": [0.4], "machine_start_gcode": "START"}

def _process():
    return {"name": "0.20mm Standard", "layer_height": 0.2, "print_speed": 200,
            "compatible_printers": ["Bambu Lab P1S 0.4 nozzle"]}

def _filament():
    return {"name": "Bambu PLA Basic", "filament_type": "PLA",
            "filament_colour": "#FFFFFF", "nozzle_temperature": 220, "bed_temperature": 35}

def test_build_sets_from_user():
    assert build_project_settings(_machine(), _process(), [_filament()])["from"] == "user"

def test_process_overrides_machine():
    machine = {**_machine(), "layer_height": 0.3}
    cfg = build_project_settings(machine, _process(), [_filament()])
    assert cfg["layer_height"] == 0.2

def test_filament_fields_become_arrays():
    cfg = build_project_settings(_machine(), _process(), [_filament(), _filament()])
    assert isinstance(cfg["filament_type"], list)
    assert cfg["filament_type"] == ["PLA", "PLA"]

def test_single_filament_produces_length_one_array():
    assert build_project_settings(_machine(), _process(), [_filament()])["filament_type"] == ["PLA"]

def test_metadata_keys_stripped():
    cfg = build_project_settings(_machine(), _process(), [_filament()])
    assert "inherits" not in cfg
    assert "compatible_printers" not in cfg

def test_embed_project_settings_creates_valid_zip(tmp_path):
    src = tmp_path / "src.3mf"
    with zipfile.ZipFile(str(src), "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    out = tmp_path / "out.3mf"
    embed_project_settings(str(src), {"from": "user", "layer_height": 0.2}, str(out))
    with zipfile.ZipFile(str(out)) as zf:
        assert "Metadata/project_settings.config" in zf.namelist()
        data = json.loads(zf.read("Metadata/project_settings.config"))
        assert data["layer_height"] == 0.2

def test_embed_preserves_model_settings(tmp_path):
    src = tmp_path / "src.3mf"
    with zipfile.ZipFile(str(src), "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/model_settings.config", json.dumps({"objects": []}))
    out = tmp_path / "out.3mf"
    embed_project_settings(str(src), {"from": "user"}, str(out))
    with zipfile.ZipFile(str(out)) as zf:
        assert "Metadata/model_settings.config" in zf.namelist()
```

- [ ] **Step 4.2: Run tests - expect ImportError/FAIL**

```bash
python -m pytest tests/test_project_config_builder.py -v 2>&1 | head -10
```

- [ ] **Step 4.3: Implement app/project_config_builder.py**

```python
"""Build OrcaSlicer project_settings.config from flattened presets and embed into 3MF."""
from __future__ import annotations

import json
import zipfile

_STRIP_KEYS = {
    "inherits", "compatible_printers", "compatible_printers_condition",
    "is_custom_defined", "from", "instantiation",
}

_KNOWN_FILAMENT_KEYS = {
    "filament_type", "filament_colour", "filament_vendor", "filament_diameter",
    "filament_density", "filament_settings_id", "nozzle_temperature",
    "nozzle_temperature_initial_layer", "nozzle_temperature_range_low",
    "nozzle_temperature_range_high", "bed_temperature", "bed_temperature_initial_layer",
    "filament_cost", "filament_spool_weight", "filament_max_volumetric_speed",
}


def build_project_settings(machine: dict, process: dict, filaments: list[dict]) -> dict:
    """Merge flattened machine + process + filament presets into a project_settings dict."""
    config: dict = {}
    config.update({k: v for k, v in machine.items() if k not in _STRIP_KEYS})
    config.update({k: v for k, v in process.items() if k not in _STRIP_KEYS})

    all_filament_keys = set(_KNOWN_FILAMENT_KEYS)
    for fil in filaments:
        all_filament_keys.update(fil.keys())
    all_filament_keys -= _STRIP_KEYS

    for key in all_filament_keys:
        values = []
        for fil in filaments:
            val = fil.get(key, "")
            if isinstance(val, list):
                val = val[0] if val else ""
            values.append(val)
        config[key] = values

    config["from"] = "user"
    config.setdefault("version", process.get("version", machine.get("version", "1.0.0")))
    return config


def embed_project_settings(src_3mf: str, project_settings: dict, dst_3mf: str) -> None:
    """Write dst_3mf as a copy of src_3mf with Metadata/project_settings.config replaced."""
    payload = json.dumps(project_settings, ensure_ascii=False, indent=2).encode("utf-8")
    with zipfile.ZipFile(src_3mf, "r") as src_zip:
        with zipfile.ZipFile(dst_3mf, "w", compression=zipfile.ZIP_DEFLATED) as dst_zip:
            for item in src_zip.infolist():
                if item.filename == "Metadata/project_settings.config":
                    continue
                dst_zip.writestr(item, src_zip.read(item.filename))
            dst_zip.writestr("Metadata/project_settings.config", payload)
```

- [ ] **Step 4.4: Run tests - expect PASS**

```bash
python -m pytest tests/test_project_config_builder.py -v
```

- [ ] **Step 4.5: Commit**

```bash
git add app/project_config_builder.py tests/test_project_config_builder.py
git commit -m "feat: add project config builder and 3MF project_settings embedder"
```

---

## Task 5: STL to 3MF Converter

**Files:**
- Create: `app/stl_to_3mf.py`
- Create: `tests/test_stl_to_3mf.py`

- [ ] **Step 5.1: Write failing tests**

```python
# tests/test_stl_to_3mf.py
import os, struct, zipfile
from app.stl_to_3mf import stl_to_3mf

def _binary_stl(tmp_path, triangles=1):
    path = str(tmp_path / "cube.stl")
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", triangles))
        for _ in range(triangles):
            f.write(struct.pack("<fff", 0, 0, 1))
            f.write(struct.pack("<fff", 0, 0, 0))
            f.write(struct.pack("<fff", 1, 0, 0))
            f.write(struct.pack("<fff", 0, 1, 0))
            f.write(struct.pack("<H", 0))
    return path

def _ascii_stl(tmp_path):
    path = str(tmp_path / "ascii.stl")
    with open(path, "w") as f:
        f.write("solid test\nfacet normal 0 0 1\n outer loop\n")
        f.write("  vertex 0 0 0\n  vertex 1 0 0\n  vertex 0 1 0\n")
        f.write(" endloop\nendfacet\nendsolid test\n")
    return path

def test_binary_stl_produces_valid_3mf(tmp_path):
    dst = str(tmp_path / "out.3mf")
    stl_to_3mf(_binary_stl(tmp_path), dst)
    with zipfile.ZipFile(dst) as zf:
        assert any("3dmodel.model" in n for n in zf.namelist())
        assert "[Content_Types].xml" in zf.namelist()

def test_ascii_stl_produces_valid_3mf(tmp_path):
    dst = str(tmp_path / "out.3mf")
    stl_to_3mf(_ascii_stl(tmp_path), dst)
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
        assert "<vertex" in xml and "<triangle" in xml

def test_vertex_count_matches_triangles(tmp_path):
    dst = str(tmp_path / "out.3mf")
    stl_to_3mf(_binary_stl(tmp_path, triangles=2), dst)
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    assert xml.count("<vertex ") == 6
```

- [ ] **Step 5.2: Run tests - expect ImportError/FAIL**

```bash
python -m pytest tests/test_stl_to_3mf.py -v 2>&1 | head -10
```

- [ ] **Step 5.3: Implement app/stl_to_3mf.py**

```python
"""Convert a bare STL (binary or ASCII) into a minimal valid 3MF."""
from __future__ import annotations

import struct
import zipfile

_CONTENT_TYPES = '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'

_RELS = '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'


def _is_binary(path: str) -> bool:
    with open(path, "rb") as f:
        return not f.read(80).startswith(b"solid")


def _parse_binary(path: str) -> list[tuple]:
    tris = []
    with open(path, "rb") as f:
        f.read(80)
        count = struct.unpack("<I", f.read(4))[0]
        for _ in range(count):
            f.read(12)
            verts = [struct.unpack("<fff", f.read(12)) for _ in range(3)]
            f.read(2)
            tris.append(tuple(verts))
    return tris


def _parse_ascii(path: str) -> list[tuple]:
    tris, verts = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("vertex"):
                parts = line.split()
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(verts) == 3:
                    tris.append(tuple(verts))
                    verts = []
    return tris


def _build_model_xml(tris: list[tuple]) -> str:
    vlines, tlines = [], []
    idx = 0
    for tri in tris:
        for x, y, z in tri:
            vlines.append(f'          <vertex x="{x}" y="{y}" z="{z}"/>')
        tlines.append(f'          <triangle v1="{idx}" v2="{idx+1}" v3="{idx+2}"/>')
        idx += 3
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        '  <resources><object id="1" type="model"><mesh>\n'
        f'    <vertices>\n{chr(10).join(vlines)}\n    </vertices>\n'
        f'    <triangles>\n{chr(10).join(tlines)}\n    </triangles>\n'
        '  </mesh></object></resources>\n'
        '  <build><item objectid="1"/></build>\n</model>'
    )


def stl_to_3mf(stl_path: str, out_path: str) -> None:
    """Convert stl_path to a minimal 3MF written at out_path."""
    tris = _parse_binary(stl_path) if _is_binary(stl_path) else _parse_ascii(stl_path)
    model_xml = _build_model_xml(tris)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("3D/3dmodel.model", model_xml.encode("utf-8"))
```

- [ ] **Step 5.4: Run tests - expect PASS**

```bash
python -m pytest tests/test_stl_to_3mf.py -v
```

- [ ] **Step 5.5: Commit**

```bash
git add app/stl_to_3mf.py tests/test_stl_to_3mf.py
git commit -m "feat: add STL-to-3MF converter"
```

---

## Task 6: Wire Catalog into main.py Lifespan and Health

**Files:**
- Modify: `app/main.py`

- [ ] **Step 6.1: Add imports and globals near the top of main.py**

After the existing imports, add:

```python
from app.profile_catalog import ProfileCatalog

SYSTEM_PROFILES_DIR = os.environ.get("SYSTEM_PROFILES_DIR", "/opt/orcaslicer/resources/profiles")
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT_SECONDS", "600"))

catalog: Optional[ProfileCatalog] = None
_catalog_building: bool = False
_orcaslicer_version: Optional[str] = None
```

- [ ] **Step 6.2: Replace lifespan function**

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global catalog, _catalog_building, _orcaslicer_version
    for d in (CONFIG_DIR, DATA_DIR, JOBS_DIR, ARRANGE_DIR):
        os.makedirs(d, exist_ok=True)
    init_config_directories()
    try:
        proc = await asyncio.create_subprocess_exec(
            "orcaslicer", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        _orcaslicer_version = stdout.decode("utf-8", errors="replace").strip().splitlines()[0]
    except Exception:
        _orcaslicer_version = None
    _catalog_building = True
    asyncio.create_task(_build_catalog())
    sweep_task = asyncio.create_task(_evict_stale_jobs())
    try:
        yield
    finally:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass


async def _build_catalog():
    global catalog, _catalog_building
    try:
        cat = ProfileCatalog(system_dir=SYSTEM_PROFILES_DIR, user_dir=USER_CONFIG_DIR)
        await asyncio.to_thread(cat.build)
        catalog = cat
        logger.info("Profile catalog ready: %s", cat.counts)
    except Exception:
        logger.exception("Catalog build failed")
    finally:
        _catalog_building = False
```

- [ ] **Step 6.3: Replace GET /api/health**

```python
@app.get("/api/health")
async def health_check():
    active = sum(1 for j in jobs.values() if j["status"] == "slicing")
    return {
        "status": "healthy",
        "orcaslicer_installed": os.path.exists("/usr/local/bin/orcaslicer"),
        "orcaslicer_version": _orcaslicer_version,
        "config_mounted": os.path.exists(CONFIG_DIR),
        "system_profiles_available": os.path.isdir(SYSTEM_PROFILES_DIR),
        "catalog_loaded": catalog is not None and catalog.is_built,
        "catalog_profile_count": catalog.counts if (catalog and catalog.is_built) else None,
        "active_jobs": active,
    }
```

- [ ] **Step 6.4: Syntax-check**

```bash
python -c "import ast; ast.parse(open('app/main.py').read()); print('OK')"
```

- [ ] **Step 6.5: Commit**

```bash
git add app/main.py
git commit -m "feat: wire ProfileCatalog into lifespan; extend /api/health"
```

---

## Task 7: Revised GET /api/profiles + New GET /api/profiles/{uuid}

**Files:**
- Modify: `app/main.py`

- [ ] **Step 7.1: Replace get_profiles and add detail endpoint**

```python
@app.get("/api/profiles")
async def get_profiles(
    manufacturer: Optional[str] = None,
    model: Optional[str] = None,
    nozzle: Optional[str] = None,
    refresh: bool = False,
):
    if refresh:
        asyncio.create_task(_build_catalog())
    if catalog is None or not catalog.is_built:
        return JSONResponse(
            status_code=503,
            content={"status": "building_catalog", "detail": "Catalog not ready. Retry shortly."},
        )
    tuple_params = [p for p in (manufacturer, model, nozzle) if p is not None]
    if tuple_params and len(tuple_params) != 3:
        raise HTTPException(
            status_code=422,
            detail="Provide all three of manufacturer, model, and nozzle together, or none.",
        )
    return catalog.as_dict(manufacturer=manufacturer, model=model, nozzle=nozzle)


@app.get("/api/profiles/{profile_uuid}")
async def get_profile_detail(profile_uuid: str):
    if catalog is None or not catalog.is_built:
        return JSONResponse(status_code=503, content={"status": "building_catalog"})
    entry = catalog.get_by_uuid(profile_uuid)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Profile UUID '{profile_uuid}' not found.")
    return catalog._public(entry)
```

- [ ] **Step 7.2: Syntax-check**

```bash
python -c "import ast; ast.parse(open('app/main.py').read()); print('OK')"
```

- [ ] **Step 7.3: Commit**

```bash
git add app/main.py
git commit -m "feat: revise GET /api/profiles with tuple fields; add GET /api/profiles/{uuid}"
```

---

## Task 8: Revised POST /api/slice/start (UUID-based)

**Files:**
- Modify: `app/main.py`

- [ ] **Step 8.1: Add new imports at top of main.py**

```python
import json as _json
from app.project_config_builder import build_project_settings, embed_project_settings
from app.stl_to_3mf import stl_to_3mf as _stl_to_3mf
```

- [ ] **Step 8.2: Replace start_slice endpoint**

```python
@app.post("/api/slice/start")
async def start_slice(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    manufacturer: str = Form(...),
    model: str = Form(...),
    nozzle: str = Form(...),
    process_uuid: str = Form(...),
    filament_uuids: str = Form(..., description='JSON array, e.g. ["uuid1"]'),
    plate: int = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: bool = Form(True),
):
    if catalog is None or not catalog.is_built:
        raise HTTPException(status_code=503, detail="Profile catalog not yet ready.")

    try:
        fil_uuid_list: list[str] = _json.loads(filament_uuids)
        if not isinstance(fil_uuid_list, list) or not fil_uuid_list:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="filament_uuids must be a non-empty JSON array.")

    if plate < 1:
        raise HTTPException(status_code=422, detail="plate must be >= 1.")

    machine_entry = catalog.get_machine(manufacturer, model, nozzle)
    if machine_entry is None:
        raise HTTPException(
            status_code=422,
            detail=f"No machine profile found for manufacturer='{manufacturer}' model='{model}' nozzle='{nozzle}'.",
        )

    process_entry = catalog.get_by_uuid(process_uuid)
    if process_entry is None or process_entry.get("type") != "process":
        raise HTTPException(status_code=422, detail=f"Process UUID '{process_uuid}' not found.")

    filament_entries = []
    for fuid in fil_uuid_list:
        fe = catalog.get_by_uuid(fuid)
        if fe is None or fe.get("type") != "filament":
            raise HTTPException(status_code=422, detail=f"Filament UUID '{fuid}' not found.")
        filament_entries.append(fe)

    machine_name = machine_entry["name"]
    compat = process_entry.get("compatible_printers", [])
    if compat and machine_name not in compat:
        raise HTTPException(
            status_code=422,
            detail=f"Process '{process_entry['name']}' is not compatible with '{machine_name}'.",
        )

    try:
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    raw_path = os.path.join(input_dir, safe_name)
    with open(raw_path, "wb") as buf:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buf)

    if safe_name.lower().endswith(".stl"):
        base_3mf = os.path.join(input_dir, os.path.splitext(safe_name)[0] + ".3mf")
        await asyncio.to_thread(_stl_to_3mf, raw_path, base_3mf)
    else:
        base_3mf = raw_path

    machine_resolved = machine_entry.get("_resolved", machine_entry)
    process_resolved = process_entry.get("_resolved", process_entry)
    filament_resolved_list = [fe.get("_resolved", fe) for fe in filament_entries]

    project_cfg = await asyncio.to_thread(
        build_project_settings, machine_resolved, process_resolved, filament_resolved_list
    )
    prepared_3mf = os.path.join(input_dir, "prepared.3mf")
    await asyncio.to_thread(embed_project_settings, base_3mf, project_cfg, prepared_3mf)

    job_logger = JobLogger(job_id)
    jobs[job_id] = {
        "id": job_id, "status": "pending",
        "input_file": prepared_3mf, "output_dir": output_dir,
        "sliced_file": None, "output_format": "gcode_3mf" if export_3mf else "gcode",
        "error": None, "logger": job_logger, "created_at": time.monotonic(),
    }
    background_tasks.add_task(
        run_orcaslicer_task,
        job_id=job_id, input_file_path=prepared_3mf, output_dir=output_dir,
        plate_id=plate, export_3mf=export_3mf, geometry_only_retry=geometry_only_retry,
    )
    return {"job_id": job_id, "status": "pending", "message": "Slicing job started."}
```

- [ ] **Step 8.3: Syntax-check**

```bash
python -c "import ast; ast.parse(open('app/main.py').read()); print('OK')"
```

- [ ] **Step 8.4: Commit**

```bash
git add app/main.py
git commit -m "feat: revise POST /api/slice/start to accept machine tuple and profile UUIDs"
```

---

## Task 9: New POST /api/slice/prepared + Rewritten run_orcaslicer_task

**Files:**
- Modify: `app/main.py`

- [ ] **Step 9.1: Replace run_orcaslicer_task**

```python
async def _strip_model_settings(src: str, dst: str) -> None:
    """Write dst as a copy of src with Metadata/model_settings.config removed."""
    import zipfile as _zf
    with _zf.ZipFile(src, "r") as s:
        with _zf.ZipFile(dst, "w", compression=_zf.ZIP_DEFLATED) as d:
            for item in s.infolist():
                if item.filename != "Metadata/model_settings.config":
                    d.writestr(item, s.read(item.filename))


async def run_orcaslicer_task(
    job_id: str,
    input_file_path: str,
    output_dir: str,
    plate_id: int = 1,
    export_3mf: Optional[str] = None,
    geometry_only_retry: bool = True,
):
    job = jobs.get(job_id)
    if job is None:
        return
    job_logger = job["logger"]
    job["status"] = "slicing"
    job_logger.log(f"Starting slice: {os.path.basename(input_file_path)}")

    async def _attempt(slice_input: str, label: str) -> bool:
        cmd = [
            "xvfb-run", "-a", "--server-args=-screen 0 1024x768x24",
            "orcaslicer", "--slice", str(plate_id),
            "--outputdir", output_dir, "--arrange", "1",
        ]
        if export_3mf:
            cmd.extend(["--export-3mf", export_3mf])
        cmd.append(slice_input)
        job_logger.log(f"{label}: {' '.join(cmd)}")
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                await asyncio.wait_for(_stream_subprocess_output(process, job_logger), timeout=SLICE_TIMEOUT)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                job_logger.log(f"ERROR: Timed out after {SLICE_TIMEOUT}s")
                return False
            await process.wait()
            return process.returncode == 0
        except Exception as exc:
            job_logger.log(f"SYSTEM ERROR: {exc}")
            logger.exception("Subprocess error in job %s", job_id)
            return False

    success = await _attempt(input_file_path, "Attempt 1")

    if not success and geometry_only_retry:
        job_logger.log("Attempt 1 failed - retrying with model_settings stripped")
        geo_path = input_file_path.replace(".3mf", "_geo.3mf")
        try:
            await _strip_model_settings(input_file_path, geo_path)
            success = await _attempt(geo_path, "Attempt 2 (geometry-only)")
        except Exception as exc:
            job_logger.log(f"ERROR stripping model_settings: {exc}")

    if success:
        if export_3mf:
            target = os.path.join(output_dir, export_3mf)
            found = target if os.path.exists(target) else None
        else:
            gcodes = sorted(glob.glob(os.path.join(output_dir, "*.gcode")))
            found = gcodes[0] if gcodes else None

        if found:
            job["status"] = "completed"
            job["sliced_file"] = found
            job_logger.log(f"Output: {os.path.basename(found)}")
            job_logger.log("__COMPLETED__")
        else:
            job["status"] = "failed"
            job["error"] = "OrcaSlicer succeeded but no output file found."
            job_logger.log("ERROR: No output file found.")
            job_logger.log("__FAILED__: Missing output file")
    else:
        job["status"] = "failed"
        job["error"] = "OrcaSlicer slice process failed. See logs."
        job_logger.log("__FAILED__: OrcaSlicer returned non-zero")
```

- [ ] **Step 9.2: Add POST /api/slice/prepared**

```python
@app.post("/api/slice/prepared")
async def slice_prepared(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    plate: int = Form(...),
    export_3mf: Optional[str] = Form(None),
    geometry_only_retry: bool = Form(True),
):
    if plate < 1:
        raise HTTPException(status_code=422, detail="plate must be >= 1.")
    try:
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not safe_name.lower().endswith(".3mf"):
        raise HTTPException(status_code=422, detail="Only .3mf files accepted here.")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    input_dir = os.path.join(job_dir, "input")
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    input_path = os.path.join(input_dir, safe_name)
    with open(input_path, "wb") as buf:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buf)

    job_logger = JobLogger(job_id)
    jobs[job_id] = {
        "id": job_id, "status": "pending",
        "input_file": input_path, "output_dir": output_dir,
        "sliced_file": None, "output_format": "gcode_3mf" if export_3mf else "gcode",
        "error": None, "logger": job_logger, "created_at": time.monotonic(),
    }
    background_tasks.add_task(
        run_orcaslicer_task,
        job_id=job_id, input_file_path=input_path, output_dir=output_dir,
        plate_id=plate, export_3mf=export_3mf, geometry_only_retry=geometry_only_retry,
    )
    return {"job_id": job_id, "status": "pending", "message": "Slice job started."}
```

- [ ] **Step 9.3: Update get_job_status to include output_format**

```python
@app.get("/api/slice/status/{job_id}")
async def get_job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")
    job = jobs[job_id]
    return {
        "job_id": job["id"],
        "status": job["status"],
        "output_format": job.get("output_format", "gcode"),
        "sliced_file": os.path.basename(job["sliced_file"]) if job["sliced_file"] else None,
        "error": job["error"],
    }
```

- [ ] **Step 9.4: Syntax-check**

```bash
python -c "import ast; ast.parse(open('app/main.py').read()); print('OK')"
```

- [ ] **Step 9.5: Commit**

```bash
git add app/main.py
git commit -m "feat: add POST /api/slice/prepared; rewrite run_orcaslicer_task with geometry-only retry"
```

---

## Task 10: Profile Upload Triggers Catalog Rebuild

**Files:**
- Modify: `app/main.py`

- [ ] **Step 10.1: Update upload_profile to trigger catalog rebuild**

```python
@app.post("/api/profiles/upload")
async def upload_profile(
    type: str = Form(...),
    file: UploadFile = File(...),
):
    if type not in ("machine", "process", "filament"):
        raise HTTPException(status_code=400, detail="type must be machine, process, or filament.")
    try:
        safe_name = _safe_filename(file.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not safe_name.endswith(".json"):
        raise HTTPException(status_code=400, detail="Profile file must be a .json file.")

    target_dir = os.path.join(USER_CONFIG_DIR, "default", type)
    os.makedirs(target_dir, exist_ok=True)
    target_file = os.path.join(target_dir, safe_name)
    with open(target_file, "wb") as buffer:
        await asyncio.to_thread(shutil.copyfileobj, file.file, buffer)

    asyncio.create_task(_build_catalog())

    return {
        "status": "success",
        "message": f"Profile uploaded to {type}/{safe_name}. Catalog rebuild started.",
        "filename": safe_name,
    }
```

- [ ] **Step 10.2: Final syntax-check**

```bash
python -c "import ast; ast.parse(open('app/main.py').read()); print('OK')"
```

- [ ] **Step 10.3: Run full test suite**

```bash
pip install pytest
python -m pytest tests/ -v
```

Expected: all PASSED

- [ ] **Step 10.4: Commit and merge**

```bash
git add app/main.py
git commit -m "feat: rebuild catalog on profile upload"
git log --oneline -8
```

---

## Spec Coverage Check

| Requirement | Covered By |
|-------------|-----------|
| REQ-PROFILES-01 Stable UUIDs | Task 2 |
| REQ-PROFILES-02 Catalog with machine tuple | Tasks 3, 7 |
| REQ-PROFILES-03 Profile detail by UUID | Task 7 |
| REQ-PROFILES-04 Filament structured fields | Task 3 (_make_entry for filament) |
| REQ-PROFILES-05 Compatible preset filtering | Task 3 (as_dict), Task 7 |
| REQ-PROFILES-06 Upload + catalog rebuild | Task 10 |
| REQ-SLICE-01 UUID-based slice start | Task 8 |
| REQ-SLICE-02 Pre-configured 3MF endpoint | Task 9 |
| REQ-SLICE-03 Dual output format | Tasks 8, 9 |
| REQ-SLICE-04 Geometry-only retry | Task 9 |
| REQ-SLICE-05 Configurable timeout (SLICE_TIMEOUT_SECONDS env var) | Task 6 |
| REQ-JOB-01 Status with output_format | Task 9 |
| REQ-JOB-02 SSE logs | Existing (unchanged) |
| REQ-JOB-03 Download | Existing (unchanged) |
| REQ-JOB-04 TTL eviction | Existing (unchanged) |
| REQ-ARRANGE-01 Arrangement | Existing (unchanged) |
| REQ-HEALTH-01 Extended health | Task 6 |
| REQ-NFR-01 503 while catalog builds | Task 7 |
