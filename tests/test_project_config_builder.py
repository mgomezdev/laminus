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


def test_empty_filaments_raises():
    with pytest.raises(ValueError, match="filament"):
        build_project_settings(_machine(), _process(), [])


def test_filament_key_omitted_when_no_filament_defines_it():
    """A filament setting no filament in the batch defines (e.g. filament_density)
    must be absent from the config, not written as [""] (regression: previously
    seeded from a fixed 'known keys' set regardless of what filaments actually had)."""
    cfg = build_project_settings(_machine(), _process(), [_filament()])
    assert "filament_density" not in cfg
    assert "filament_vendor" not in cfg


def test_filament_key_present_when_at_least_one_filament_defines_it():
    fil_with_density = {**_filament(), "filament_density": 1.24}
    cfg = build_project_settings(_machine(), _process(), [_filament(), fil_with_density])
    assert cfg["filament_density"] == ["", 1.24]


def test_layer_gcode_injected_only_for_elegoo_centauri_carbon():
    assert "layer_gcode" not in build_project_settings(_machine(), _process(), [_filament()])
    cc = {**_machine(), "printer_model": "Elegoo Centauri Carbon"}
    assert build_project_settings(cc, _process(), [_filament()])["layer_gcode"] == "G92 E0\n"


def test_strip_application_version_g92_added_only_for_cc(tmp_path):
    from app.stl_to_3mf import strip_application_version

    def run(model):
        src, dst = tmp_path / f"{model}.in.3mf", tmp_path / f"{model}.out.3mf"
        cfg = {"use_relative_e_distances": "1", "printer_model": model,
               "layer_change_gcode": ";AFTER_LAYER_CHANGE"}
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("Metadata/project_settings.config", json.dumps(cfg))
        strip_application_version(str(src), str(dst))
        with zipfile.ZipFile(dst) as z:
            return json.loads(z.read("Metadata/project_settings.config"))

    other = run("Snapmaker U1")
    assert other["layer_change_gcode"] == ";AFTER_LAYER_CHANGE"
    cc = run("Elegoo Centauri Carbon")
    assert cc["use_relative_e_distances"] == "1" and "G92 E0" in cc["layer_change_gcode"]
