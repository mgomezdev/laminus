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


def fake_pack_export(n_instances: int, plates: "list[list[int]] | None" = None) -> bytes:
    """Minimal stand-in for OrcaSlicer's `/pack` --export-3mf output.

    One object per input copy (ids 2, 4, ...), whose part records its input position as
    `source_object_id`, as Orca does. *plates*: input positions per plate (default: one
    plate holding every copy in input order).
    """
    import io
    import zipfile
    plates = plates if plates is not None else [list(range(n_instances))]
    oid = {k: str(2 * (k + 1)) for k in range(n_instances)}
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model xmlns='
             '"http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             + "".join(f'<object id="{oid[k]}" type="model"/>' for k in range(n_instances))
             + "</resources><build>"
             + "".join(f'<item objectid="{oid[k]}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
                       for k in range(n_instances))
             + "</build></model>")
    ms = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>'
          + "".join(f'<object id="{oid[k]}"><metadata key="name" value="obj{k}"/>'
                    f'<part id="1"><metadata key="source_object_id" value="{k}"/></part></object>'
                    for k in range(n_instances))
          + "".join(f'<plate><metadata key="plater_id" value="{p + 1}"/>'
                    + "".join(f'<model_instance><metadata key="object_id" value="{oid[k]}"/>'
                              f'<metadata key="instance_id" value="0"/></model_instance>' for k in ks)
                    + "</plate>" for p, ks in enumerate(plates))
          + "</config>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", ms)
    return buf.getvalue()
