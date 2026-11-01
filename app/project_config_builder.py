"""Build OrcaSlicer project_settings.config from flattened presets and embed into 3MF."""
from __future__ import annotations

import json
import os
import re
import zipfile

_STRIP_KEYS = {
    "inherits", "compatible_printers", "compatible_printers_condition",
    "is_custom_defined", "from", "instantiation",
}

def build_project_settings(machine: dict, process: dict, filaments: list[dict]) -> dict:
    """Merge flattened machine + process + filament presets into a project_settings dict."""
    if not filaments:
        raise ValueError("filaments must contain at least one filament preset")
    config: dict = {}
    config.update({k: v for k, v in machine.items() if k not in _STRIP_KEYS})
    config.update({k: v for k, v in process.items() if k not in _STRIP_KEYS})

    # Only emit a key when at least one filament actually defines it - seeding this
    # with a fixed "known keys" set previously wrote missing settings (e.g.
    # nozzle_temperature, filament_density) to the slicer as [""] instead of omitting
    # them.
    all_filament_keys: set[str] = set()
    for fil in filaments:
        all_filament_keys.update(fil.keys())
    all_filament_keys -= _STRIP_KEYS

    for key in all_filament_keys:
        values = []
        for fil in filaments:
            val = fil.get(key, "")
            while isinstance(val, list):
                val = val[0] if val else ""
            values.append(val)
        config[key] = values

    normalize_filament_ids(config, len(filaments))

    config["from"] = "user"
    config.setdefault("version", process.get("version", machine.get("version", "1.0.0")))
    ensure_extruder_reset_per_layer(config)
    return config


def normalize_filament_ids(config: dict, filament_count: int | None = None) -> bool:
    """BIZ-351: projects carry each filament's id as ``filament_ids``, never the preset-level
    ``filament_id``. OrcaSlicer 2.4.2 segfaults in --export-3mf if ``filament_id`` is present
    or ``filament_ids`` is missing/empty; "" is accepted for a filament without an id.
    Fixes *config* in place and returns whether anything changed."""
    preset_ids = config.pop("filament_id", None)
    if config.get("filament_ids"):
        return preset_ids is not None
    if isinstance(preset_ids, list) and preset_ids:
        config["filament_ids"] = preset_ids
    else:
        if filament_count is None:
            types = config.get("filament_type")
            filament_count = len(types) if isinstance(types, list) else 0
        config["filament_ids"] = [""] * max(filament_count, 1)
    return True


_G92_E0 = re.compile(r"^[ \t]*G92[ \t]*E(0(\.0*)?|\.0+)[ \t]*(;.*)?$", re.MULTILINE)


def ensure_extruder_reset_per_layer(config: dict) -> bool:
    """Mirror OrcaSlicer 2.4.2's -51 check (Print::validate): relative extrusion
    (``use_relative_e_distances``, default true) on a marlin/marlin2 flavor needs an exact
    ``G92 E0`` in ``before_layer_change_gcode`` or ``layer_change_gcode`` - the error text
    names the legacy ``layer_gcode``, which it does not read. Orca exempts BBL printers but
    does not recognise one in a Laminus-built project, so Bambu gets the reset too. Klipper
    needs none, and absolute extrusion rejects one, so both are left alone.
    Fixes *config* in place and returns whether anything changed."""
    relative = str(config.get("use_relative_e_distances", "1")).lower() not in ("0", "false")
    if not relative or config.get("gcode_flavor", "marlin") not in ("marlin", "marlin2"):
        return False
    before = config.get("before_layer_change_gcode") or ""
    if _G92_E0.search(before) or _G92_E0.search(config.get("layer_change_gcode") or ""):
        return False
    config["before_layer_change_gcode"] = (before.rstrip("\n") + "\n" if before else "") + "G92 E0\n"
    return True


def embed_project_settings(src_3mf: str, project_settings: dict, dst_3mf: str) -> None:
    """Write dst_3mf as a copy of src_3mf with Metadata/project_settings.config replaced."""
    if os.path.realpath(src_3mf) == os.path.realpath(dst_3mf):
        raise ValueError("src_3mf and dst_3mf must be different paths")
    payload = json.dumps(project_settings, ensure_ascii=False, indent=2).encode("utf-8")
    with zipfile.ZipFile(src_3mf, "r") as src_zip:
        with zipfile.ZipFile(dst_3mf, "w", compression=zipfile.ZIP_DEFLATED) as dst_zip:
            for item in src_zip.infolist():
                if item.filename == "Metadata/project_settings.config":
                    continue
                dst_zip.writestr(item, src_zip.read(item.filename))
            dst_zip.writestr("Metadata/project_settings.config", payload)
