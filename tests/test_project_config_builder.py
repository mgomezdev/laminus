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


# BIZ-350: Orca 2.4.2 (Print.cpp) -51s when extrusion is relative - `use_relative_e_distances`,
# default true - on a marlin/marlin2 flavor and neither before_layer_change_gcode nor
# layer_change_gcode has an exact "G92 E0". It skips that for BBL printers, but never
# recognises one in a Laminus-built project.

@pytest.mark.parametrize("flavor", ["marlin", "marlin2"])
def test_marlin_with_default_relative_gets_extruder_reset(flavor):
    # Bambu P1S (marlin) and Creality Ender-3 V3 SE (marlin2) leave the key unset.
    cfg = build_project_settings({**_machine(), "gcode_flavor": flavor}, _process(), [_filament()])
    assert cfg["before_layer_change_gcode"] == "G92 E0\n"


def test_explicit_relative_extrusion_gets_extruder_reset_and_keeps_existing_gcode():
    machine = {**_machine(), "gcode_flavor": "marlin", "use_relative_e_distances": "1",
               "before_layer_change_gcode": "; mine\n"}
    gcode = build_project_settings(machine, _process(), [_filament()])["before_layer_change_gcode"]
    assert gcode == "; mine\nG92 E0\n"


@pytest.mark.parametrize("key", ["before_layer_change_gcode", "layer_change_gcode"])
def test_extruder_reset_not_added_when_either_hook_has_it(key):
    machine = {**_machine(), "gcode_flavor": "marlin", key: ";x\nG92 E0 ; reset\n"}
    cfg = build_project_settings(machine, _process(), [_filament()])
    assert cfg.get("before_layer_change_gcode", "").count("G92 E0") == (key == "before_layer_change_gcode")


@pytest.mark.parametrize("machine", [
    {"gcode_flavor": "klipper"},  # Elegoo Centauri Carbon, Snapmaker U1: Orca needs no reset
    {"gcode_flavor": "marlin2", "use_relative_e_distances": "0"},  # absolute: a reset is an error
])
def test_extruder_reset_left_alone_when_orca_does_not_need_it(machine):
    cfg = build_project_settings({**_machine(), **machine}, _process(), [_filament()])
    assert "before_layer_change_gcode" not in cfg


def test_no_legacy_layer_gcode_key():
    # Orca reads layer_change_gcode; `layer_gcode` was a dead default.
    assert "layer_gcode" not in build_project_settings(_machine(), _process(), [_filament()])


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


def test_filament_id_written_as_project_level_filament_ids():
    # BIZ-351: Orca 2.4.2 segfaults in --export-3mf when a project carries the preset-level
    # `filament_id` key or lacks a non-empty `filament_ids`; Orca-saved projects only have the latter.
    cfg = build_project_settings(_machine(), _process(),
                                 [{**_filament(), "filament_id": "GFA00"}, _filament()])
    assert cfg["filament_ids"] == ["GFA00", ""]
    assert "filament_id" not in cfg


def test_filament_ids_present_when_no_filament_defines_an_id():
    cfg = build_project_settings(_machine(), _process(), [_filament()])
    assert cfg["filament_ids"] == [""]


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
