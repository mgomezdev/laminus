"""Subset extraction: pick objects x qty out of an Orca/Bambu 3MF, keep overrides intact."""
import io
import json
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import subset_3mf as s
from app.main import app

MODEL_NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
    'requiredextensions="p"'
)


def _mesh(paint: str = "") -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {MODEL_NS}><resources>'
        '<object id="1" type="model"><mesh><vertices>'
        '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/>'
        f'</vertices><triangles><triangle v1="0" v2="1" v3="2" {paint}/></triangles></mesh></object>'
        '</resources></model>'
    )


def make_3mf(path, extra_project=None):
    """Two objects: 'Lid' (painted supports, modifier part, height range) and 'Base'."""
    project = {"printable_area": ["0x0", "100x0", "100x100", "0x100"], "printable_height": "50",
               "printer_settings_id": "Old Printer", "print_compatible_printers": ["Old Printer"],
               "raft_first_layer_expansion": "-1"}
    project.update(extra_project or {})
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {MODEL_NS}>'
        '<metadata name="Application">BambuStudio-99.0</metadata><resources>'
        '<object id="2" p:UUID="u2" type="model"><components>'
        '<component p:path="/3D/Objects/lid.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        '</components></object>'
        '<object id="4" p:UUID="u4" type="model"><components>'
        '<component p:path="/3D/Objects/base.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        '</components></object>'
        '</resources><build>'
        '<item objectid="2" p:UUID="b2" transform="1 0 0 0 1 0 0 0 1 50 50 0" printable="1"/>'
        '<item objectid="4" p:UUID="b4" transform="1 0 0 0 1 0 0 0 1 150 50 0" printable="1"/>'
        '</build></model>'
    )
    settings = (
        '<?xml version="1.0" encoding="UTF-8"?><config>'
        '<object id="2"><metadata key="name" value="Lid"/><metadata key="extruder" value="2"/>'
        '<metadata key="wall_loops" value="6"/>'
        '<part id="1" subtype="normal_part"><metadata key="name" value="Lid body"/></part>'
        '<part id="3" subtype="modifier_part"><metadata key="name" value="Mod"/>'
        '<metadata key="sparse_infill_density" value="50%"/></part></object>'
        '<object id="4"><metadata key="name" value="Base"/><metadata key="extruder" value="1"/>'
        '<part id="5" subtype="normal_part"><metadata key="name" value="Base"/></part></object>'
        '<plate><metadata key="plater_id" value="1"/><metadata key="thumbnail_file" value="Metadata/plate_1.png"/>'
        '<model_instance><metadata key="object_id" value="2"/><metadata key="instance_id" value="0"/></model_instance>'
        '<model_instance><metadata key="object_id" value="4"/><metadata key="instance_id" value="0"/></model_instance>'
        '</plate>'
        '<assemble><assemble_item object_id="2" instance_id="0" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        '<assemble_item object_id="4" instance_id="0" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></assemble>'
        '</config>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/Objects/lid.model" Id="rel-1" Type="t"/>'
        '<Relationship Target="/3D/Objects/base.model" Id="rel-2" Type="t"/></Relationships>'
    )
    ranges = (
        '<objects><object id="2"><range min_z="1" max_z="2"><option opt_key="layer_height">0.1</option></range></object>'
        '<object id="4"><range min_z="1" max_z="2"><option opt_key="layer_height">0.3</option></range></object></objects>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", model)
        z.writestr("3D/_rels/3dmodel.model.rels", rels)
        z.writestr("3D/Objects/lid.model", _mesh('paint_supports="4C" paint_color="8"'))
        z.writestr("3D/Objects/base.model", _mesh())
        z.writestr("Metadata/model_settings.config", settings)
        z.writestr("Metadata/project_settings.config", json.dumps(project))
        z.writestr("Metadata/layer_config_ranges.xml", ranges)
        z.writestr("Metadata/plate_1.png", b"png")
        z.writestr("Metadata/custom_gcode_per_layer.xml", "<x/>")


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "src.3mf"
    make_3mf(p)
    return str(p)


def test_list_objects(src):
    objs = {o["name"]: o for o in s.list_objects(src)}
    assert objs["Lid"]["id"] == 2
    assert objs["Lid"]["extruder"] == "2"
    assert objs["Lid"]["overrides"] == ["wall_loops"]
    assert [p["subtype"] for p in objs["Lid"]["parts"]] == ["normal_part", "modifier_part"]


def test_subset_keeps_only_selected_with_quantities(src, tmp_path):
    dst = str(tmp_path / "out.3mf")
    want = s.resolve_selection(s.list_objects(src), [{"name": "Lid", "qty": 3}])
    summary = s.build_subset(src, dst, want)
    assert summary["instances"] == 3
    fp = s.fingerprint(dst)
    assert list(fp) == ["Lid#2"]
    assert fp["Lid#2"]["instances"] == 3
    with zipfile.ZipFile(dst) as z:
        names = z.namelist()
        assert "3D/Objects/lid.model" in names
        assert "3D/Objects/base.model" not in names
        assert b"base.model" not in z.read("3D/_rels/3dmodel.model.rels")
        assert b'id="4"' not in z.read("Metadata/layer_config_ranges.xml")
        # stale per-plate artifacts dropped, single plate lists every instance
        assert "Metadata/plate_1.png" not in names
        assert "Metadata/custom_gcode_per_layer.xml" not in names
        ms = z.read("Metadata/model_settings.config").decode()
        assert ms.count("<model_instance") == 3
        assert 'object_id" value="4"' not in ms and 'object_id="4"' not in ms
        assert "thumbnail_file" not in ms
        assert s.count_plates(dst) == 1


def test_overrides_paint_and_modifiers_preserved(src, tmp_path):
    dst = str(tmp_path / "out.3mf")
    want = s.resolve_selection(s.list_objects(src), [{"id": 2, "qty": 2}, {"id": 4, "qty": 1}])
    s.build_subset(src, dst, want)
    a, b = s.fingerprint(src), s.fingerprint(dst)
    for key in a:
        for field in ("meta", "parts", "paint", "ranges"):
            assert a[key][field] == b[key][field], (key, field)
    assert b["Lid#2"]["paint"]["paint_supports"] == 1
    assert b["Lid#2"]["paint"]["paint_color"] == 1
    # mesh bytes untouched
    with zipfile.ZipFile(src) as zs, zipfile.ZipFile(dst) as zd:
        assert zs.read("3D/Objects/lid.model") == zd.read("3D/Objects/lid.model")


def test_application_version_stamp_dropped(src, tmp_path):
    dst = str(tmp_path / "out.3mf")
    s.build_subset(src, dst, {2: 1})
    assert b"BambuStudio-99.0" in zipfile.ZipFile(src).read("3D/3dmodel.model")
    assert b"BambuStudio-99.0" not in zipfile.ZipFile(dst).read("3D/3dmodel.model")


def test_instances_get_distinct_build_uuids(src, tmp_path):
    dst = str(tmp_path / "out.3mf")
    s.build_subset(src, dst, {2: 4})
    with zipfile.ZipFile(dst) as z:
        model = z.read("3D/3dmodel.model").decode()
    import re
    uuids = re.findall(r'<item [^>]*UUID="([^"]+)"', model)
    assert len(uuids) == 4 and len(set(uuids)) == 4


def test_printer_override_and_bambu_clamp(src, tmp_path):
    dst = str(tmp_path / "out.3mf")
    machine = {"name": "New Printer", "printable_area": ["0x0", "256x0", "256x256", "0x256"],
               "printable_height": "256"}
    s.build_subset(src, dst, {2: 1}, printer_cfg=machine)
    cfg = json.loads(zipfile.ZipFile(dst).read("Metadata/project_settings.config"))
    assert cfg["printable_area"][1] == "256x0"
    assert cfg["printable_height"] == "256"
    assert cfg["printer_settings_id"] == "New Printer"
    assert cfg["print_compatible_printers"] == []
    assert cfg["raft_first_layer_expansion"] == "0"


def test_no_printer_keeps_project_bed(src, tmp_path):
    dst = str(tmp_path / "out.3mf")
    s.build_subset(src, dst, {2: 1})
    cfg = json.loads(zipfile.ZipFile(dst).read("Metadata/project_settings.config"))
    assert cfg["printable_area"][1] == "100x0"
    assert cfg["print_compatible_printers"] == ["Old Printer"]


@pytest.mark.parametrize("selection,msg", [
    ([], "empty"),
    ([{"name": "Nope", "qty": 1}], "No object named"),
    ([{"id": 99, "qty": 1}], "No object with id"),
    ([{"name": "Lid", "qty": 0}], "qty must be"),
    ([{"qty": 1}], "needs 'id' or 'name'"),
])
def test_bad_selection(src, selection, msg):
    with pytest.raises(s.SubsetError, match=msg):
        s.resolve_selection(s.list_objects(src), selection)


def test_ambiguous_name_rejected(tmp_path):
    p = tmp_path / "dup.3mf"
    make_3mf(p)
    # rename Base -> Lid so two objects share a name
    zin = zipfile.ZipFile(p)
    data = {n: zin.read(n) for n in zin.namelist()}
    zin.close()
    data["Metadata/model_settings.config"] = data["Metadata/model_settings.config"].replace(
        b'value="Base"/><metadata key="extruder"', b'value="Lid"/><metadata key="extruder"')
    with zipfile.ZipFile(p, "w") as z:
        for n, d in data.items():
            z.writestr(n, d)
    with pytest.raises(s.SubsetError, match="ambiguous"):
        s.resolve_selection(s.list_objects(str(p)), [{"name": "Lid", "qty": 1}])
    assert s.resolve_selection(s.list_objects(str(p)), [{"id": 4, "qty": 2}]) == {4: 2}


def test_quantities_for_same_object_accumulate(src):
    assert s.resolve_selection(s.list_objects(src), [{"id": 2, "qty": 1}, {"name": "Lid", "qty": 2}]) == {2: 3}


def test_xml_with_doctype_rejected(src, tmp_path):
    evil = tmp_path / "evil.3mf"
    zin = zipfile.ZipFile(src)
    data = {n: zin.read(n) for n in zin.namelist()}
    zin.close()
    data["Metadata/model_settings.config"] = b'<!DOCTYPE x [<!ENTITY a "b">]>' + data["Metadata/model_settings.config"]
    with zipfile.ZipFile(evil, "w") as z:
        for n, d in data.items():
            z.writestr(n, d)
    with pytest.raises(s.SubsetError, match="DOCTYPE"):
        s.list_objects(str(evil))



# ------------------------------------------------------------- oracle + transplant

def _fake_two_plates(oracle_path, out_path):
    """Stand-in for `orcaslicer --arrange`: first 2 objects stay, the rest move to plate 2."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(oracle_path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    model = ET.fromstring(files[s.MODEL])
    ms = ET.fromstring(files[s.MODEL_SETTINGS])
    items = model.find(s._q("build")).findall(s._q("item"))
    for i, it in enumerate(items):
        v = it.get("transform").split()
        v[9] = str(float(v[9]) + (400 if i >= 2 else 0) + i)  # distinct x per instance
        it.set("transform", " ".join(v))
    plate = ms.find("plate")
    insts = plate.findall("model_instance")
    plate2 = ET.SubElement(ms, "plate")
    ET.SubElement(plate2, "metadata", {"key": "plater_id", "value": "2"})
    for mi in insts[2:]:
        plate.remove(mi)
        plate2.append(mi)
    files[s.MODEL] = ET.tostring(model)
    files[s.MODEL_SETTINGS] = ET.tostring(ms)
    with zipfile.ZipFile(out_path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)


def test_expand_instances_makes_unique_named_objects(src, tmp_path):
    sub, exp = str(tmp_path / "sub.3mf"), str(tmp_path / "exp.3mf")
    s.build_subset(src, sub, {2: 3, 4: 1})
    s.expand_instances(sub, exp)
    fp = s.fingerprint(exp)
    assert sorted(fp) == [f"lmns:2:{k}#{1000 + k}" for k in range(3)] + ["lmns:4:0#1003"]
    assert all(v["instances"] == 1 for v in fp.values())
    # per-object overrides + modifier part copied onto every expanded object
    assert all(("wall_loops", "6") in fp[k]["meta"] for k in fp if k.startswith("lmns:2"))
    assert s.count_plates(exp) == 1


def test_transplant_layout_moves_plates_and_transforms_but_keeps_bytes(src, tmp_path):
    sub, exp, arr, fin = (str(tmp_path / n) for n in ("sub.3mf", "exp.3mf", "arr.3mf", "fin.3mf"))
    s.build_subset(src, sub, {2: 3, 4: 1})
    s.expand_instances(sub, exp)
    _fake_two_plates(exp, arr)
    assert s.transplant_layout(sub, arr, fin) == 2
    a, b = s.fingerprint(sub), s.fingerprint(fin)
    assert a == b  # names, overrides, parts, paint, instance counts identical
    assert s.count_plates(fin) == 2
    with zipfile.ZipFile(sub) as zs, zipfile.ZipFile(fin) as zf:
        assert zs.read("3D/Objects/lid.model") == zf.read("3D/Objects/lid.model")
        ms = zf.read(s.MODEL_SETTINGS).decode()
        model = zf.read(s.MODEL).decode()
    assert ms.count("<model_instance") == 4 and 'value="1"' in ms
    import re
    xs = sorted(float(t.split()[9]) for t in re.findall(r'<item [^>]*transform="([^"]+)"', model))
    assert xs[-2] > 400  # instances 2,3 were moved to plate 2's coordinates


def test_transplant_rejects_mismatched_arrangement(src, tmp_path):
    sub, exp, other, fin = (str(tmp_path / n) for n in ("sub.3mf", "exp.3mf", "other.3mf", "fin.3mf"))
    s.build_subset(src, sub, {2: 3})
    s.build_subset(src, other, {2: 2})
    s.expand_instances(other, exp)
    with pytest.raises(s.SubsetError):
        s.transplant_layout(sub, exp, fin)

# ------------------------------------------------------------------ endpoint

def _upload(src_path):
    return ("file", ("model.3mf", open(src_path, "rb"), "application/octet-stream"))


def test_endpoint_list_objects(src):
    r = TestClient(app).post("/api/3mf/objects", files=[_upload(src)])
    assert r.status_code == 200
    assert {o["name"] for o in r.json()["objects"]} == {"Lid", "Base"}


def test_endpoint_subset_runs_orca_on_expanded_oracle(src):
    captured = {}

    async def _fake_orca(*args, **kwargs):
        a = list(args)
        out = a[a.index("--export-3mf") + 1]
        oracle = a[-1]
        captured["cmd"] = a
        captured["names"] = sorted(s.fingerprint(oracle))
        # "arrange": put instances 0-1 on plate 1 and the rest on plate 2, shifted in X
        _fake_two_plates(oracle, out)
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"ok", None))
        return proc

    with patch("asyncio.create_subprocess_exec", new=_fake_orca):
        r = TestClient(app).post(
            "/api/arrange/subset", files=[_upload(src)],
            data={"selection": json.dumps([{"name": "Lid", "qty": 5}]), "allow_rotations": "true"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Instance-Count"] == "5"
    assert r.headers["X-Plate-Count"] == "2"
    assert "--arrange" in captured["cmd"] and "--allow-rotations" in captured["cmd"]
    assert len(captured["names"]) == 5 and all(n.startswith("lmns:2:") for n in captured["names"])
    fp = s.fingerprint(io.BytesIO(r.content))
    assert fp["Lid#2"]["instances"] == 5
    assert fp["Lid#2"]["paint"]["paint_supports"] == 1  # untouched by the layout step


@pytest.mark.parametrize("selection,code", [
    ("not json", 422),
    ('{"id": 2}', 422),
    ('[{"name": "Nope", "qty": 1}]', 422),
])
def test_endpoint_rejects_bad_selection(src, selection, code):
    r = TestClient(app).post("/api/arrange/subset", files=[_upload(src)], data={"selection": selection})
    assert r.status_code == code


def test_endpoint_rejects_non_3mf():
    r = TestClient(app).post(
        "/api/arrange/subset", files=[("file", ("m.stl", b"x", "application/octet-stream"))],
        data={"selection": "[]"})
    assert r.status_code == 400
