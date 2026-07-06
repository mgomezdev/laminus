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
