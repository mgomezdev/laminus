"""BIZ-407 unit checks (no OrcaSlicer): forced label settings, G-code parsing, manifest."""
import json
import zipfile

import pytest

from app import object_labels as ol

KLIPPER = """; header
EXCLUDE_OBJECT_DEFINE NAME=a_id_0_copy_0 CENTER=10,10 POLYGON=[[5,5],[15,5],[15,15],[5,15],[5,5]]
EXCLUDE_OBJECT_DEFINE NAME=a_id_1_copy_0 CENTER=50,10 POLYGON=[[45,5],[55,5],[55,15],[45,15],[45,5]]
EXCLUDE_OBJECT_START NAME=a_id_0_copy_0
G1 X5 Y5 E.1
G1 X15 Y15 E.2
EXCLUDE_OBJECT_END NAME=a_id_0_copy_0
"""
MARLIN = """M486 S0
M486 Aa_id_0_copy_0
M486 S-1
M486 S1
M486 Aa_id_1_copy_0
M486 S0
G1 X5 Y5 E.1
G1 X15 Y15 E.1
M486 S-1
M486 S1
G1 X40 Y2 E.1
G1 X60 Y8 E.1
M486 S-1
"""
SETTINGS = {"printable_area": ["0x0", "100x0", "100x100", "0x100"]}


def _3mf(path, cfg):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(cfg))
        z.writestr("3D/3dmodel.model", "<model/>")


@pytest.mark.parametrize("off", [{"gcode_label_objects": "0", "exclude_object": "0"}, {}])
def test_force_settings_overrides_the_preset(tmp_path, off):
    p = str(tmp_path / "p.3mf")
    _3mf(p, {"layer_height": "0.2", **off})
    ol.force_settings(p)
    cfg = ol.read_threemf_settings(p)
    assert cfg["exclude_object"] == "1" and cfg["gcode_label_objects"] == "1"
    assert cfg["layer_height"] == "0.2"


def test_klipper_manifest_uses_orcas_polygon():
    m = ol.build_manifest(3, KLIPPER, SETTINGS)
    pl = m["plates"][0]
    assert pl["plate"] == 3 and pl["bed"] == {"origin": [0.0, 0.0], "size": [100.0, 100.0]}
    assert [o["label"] for o in pl["objects"]] == ["a_id_0_copy_0", "a_id_1_copy_0"]
    assert pl["objects"][0]["bbox"] == [5, 5, 15, 15]


def test_marlin_manifest_uses_extrusion_extent():
    o = ol.build_manifest(1, MARLIN, SETTINGS)["plates"][0]["objects"]
    assert o[1]["bbox"] == [40, 2, 60, 8]
    assert o[0]["label"] != o[1]["label"]


def test_missing_or_duplicate_labels_are_errors():
    with pytest.raises(ol.LabelError):
        ol.build_manifest(1, "G28\n", SETTINGS)
    with pytest.raises(ol.LabelError):
        ol.build_manifest(1, KLIPPER.replace("a_id_1_copy_0", "a_id_0_copy_0"), SETTINGS)


def test_plate_names_are_rewritten_to_labels_by_footprint(tmp_path):
    p = str(tmp_path / "o.3mf")
    pj = {"bbox_objects": [{"name": "a", "bbox": [45, 5, 55, 15]}, {"name": "a", "bbox": [5, 5, 15, 15]}]}
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/plate_1.json", json.dumps(pj))
    ol.align_plate_names(p, ol.build_manifest(1, KLIPPER, SETTINGS))
    with zipfile.ZipFile(p) as z:
        names = [e["name"] for e in json.loads(z.read("Metadata/plate_1.json"))["bbox_objects"]]
    assert names == ["a_id_1_copy_0", "a_id_0_copy_0"]
