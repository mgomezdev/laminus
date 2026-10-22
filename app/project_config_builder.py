"""Build OrcaSlicer project_settings.config from flattened presets and embed into 3MF."""
from __future__ import annotations

import json
import os
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

    config["from"] = "user"
    config.setdefault("version", process.get("version", machine.get("version", "1.0.0")))
    # OrcaSlicer CLI -51: if machine_start_gcode uses M83 (relative extrusion),
    # layer_gcode must contain G92 E0 or the slicer aborts. Docker AppImage profiles
    # omit the key entirely; inject the minimal safe default when absent.
    config.setdefault("layer_gcode", "G92 E0\n")
    _ensure_extruder_reset_per_layer(config)
    return config


def _ensure_extruder_reset_per_layer(config: dict) -> None:
    """OrcaSlicer 2.4 refuses to slice with relative extruder addressing (-51) unless
    ``before_layer_change_gcode`` contains ``G92 E0`` - the error text names the legacy
    ``layer_gcode`` key, which the slicer no longer reads. Klipper presets (e.g. Elegoo
    Centauri Carbon) are treated as relative even with ``use_relative_e_distances`` off, and
    their vendor presets leave this key unset. A per-layer extruder reset is a no-op for
    relative addressing, so add it whenever relative addressing applies."""
    relative = (str(config.get("use_relative_e_distances", "")).lower() in ("1", "true")
                or config.get("gcode_flavor") == "klipper")
    if not relative:
        return
    current = config.get("before_layer_change_gcode") or ""
    if "G92 E0" not in current:
        config["before_layer_change_gcode"] = (current.rstrip("\n") + "\n" if current else "") + "G92 E0\n"


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
