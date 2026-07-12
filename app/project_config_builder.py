"""Build OrcaSlicer project_settings.config from flattened presets and embed into 3MF."""
from __future__ import annotations

import json
import os
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
    if not filaments:
        raise ValueError("filaments must contain at least one filament preset")
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
            while isinstance(val, list):
                val = val[0] if val else ""
            values.append(val)
        config[key] = values

    config["from"] = "user"
    config.setdefault("version", process.get("version", machine.get("version", "1.0.0")))
    # OrcaSlicer CLI -51: if machine_start_gcode uses M83 (relative extrusion), the
    # layer_gcode key must be present or the slicer aborts. Desktop profiles define it
    # as an empty string; Docker AppImage profiles omit it entirely.
    config.setdefault("layer_gcode", "")
    return config


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
