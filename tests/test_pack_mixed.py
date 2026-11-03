"""BIZ-356: POST /api/v1/pack/mixed - a mixed selection on the fewest plates of one bed."""
import io
import json
import zipfile
from collections import Counter
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import manifest as mf
from app import pack_mixed as pm
from app import plate_extract as pe
from app.main import app
from tests.test_pack_next import _Catalog, _box_stl, _bundle_3mf, _fake_orca, _plates

# the BIZ-356 acceptance selection: STL x3, 3MF object x2, 3MF plate bundle (together) x2,
# solo STL x2
SEL = [{"file": "b.stl", "qty": 3},
       {"file": "a.3mf", "id": 2, "qty": 2},
       {"file": "a.3mf", "plate": 1, "qty": 2, "rule": "together"},
       {"file": "c.stl", "qty": 2, "rule": "solo"}]


def _post(tmp_path, sel=SEL, **data):
    a = tmp_path / "a.3mf"
    _bundle_3mf(a)
    files = [("files", ("a.3mf", a.read_bytes(), "application/octet-stream")),
             ("files", ("b.stl", _box_stl(40, 40, 20), "application/octet-stream")),
             ("files", ("c.stl", _box_stl(50, 50, 50), "application/octet-stream"))]
    data = data or {"bed_x": "256", "bed_y": "256", "bed_z": "256"}
    with patch("app.main.catalog", _Catalog()), patch("asyncio.create_subprocess_exec", new=_fake_orca):
        return TestClient(app).post("/api/v1/pack/mixed", files=files,
                                    data={"selection": json.dumps(sel), **data})


def _check(r, tmp_path, bed_w):
    """Valid multi-plate project: manifest sums to the request, every plate fits the bed on
    its own, together copies whole, solo instances alone. Returns the manifest."""
    assert r.status_code == 200, r.text
    content = r.content
    m = json.loads(zipfile.ZipFile(io.BytesIO(content)).read(mf.MANIFEST))
    assert [i["requested"] for i in m["items"]] == [3, 2, 4, 2]
    assert int(r.headers["X-Plate-Count"]) == len(m["plates"]) == len(_plates(content))
    got = Counter(i["item"] for pl in m["plates"] for i in pl["instances"])
    assert got == {0: 3, 1: 2, 2: 4, 3: 2}
    out = tmp_path / "out.3mf"
    out.write_bytes(content)
    for pl in m["plates"]:
        insts = pl["instances"]
        assert Counter(i["object_id"] for i in insts) == _plates(content)[pl["plate"] - 1]
        bundle = Counter(i["part"] for i in insts if i["item"] == 2)
        assert bundle["4"] == bundle["6"]              # never half a together copy
        if any(i["item"] == 3 for i in insts):
            assert len(insts) == 1                      # solo: alone on its plate
        one = tmp_path / f"plate{pl['plate']}.3mf"
        pe.extract_plate(str(out), pl["plate"], str(one))
        with zipfile.ZipFile(one) as z:
            x0, y0, x1, y1, _ = pe.plate_footprint(z)
        assert x0 >= -0.01 and y0 >= -0.01 and x1 <= bed_w + 0.01 and y1 <= bed_w + 0.01
        if insts[0]["item"] == 3:
            assert (x0 + x1) / 2 == pytest.approx(bed_w / 2) and (y0 + y1) / 2 == pytest.approx(bed_w / 2)
    return m


def test_acceptance_selection_on_256_bed(tmp_path):
    m = _check(_post(tmp_path), tmp_path, 256)
    # everything but the solo parts shares plate 1; one plate per solo instance after it
    assert len(m["plates"]) == 3
    assert {i["item"] for i in m["plates"][0]["instances"]} == {0, 1, 2}
    parts = {(i["item"], i["part"]) for pl in m["plates"] for i in pl["instances"]}
    assert (0, "b.stl") in parts and (3, "c.stl") in parts and (1, "2") in parts


def test_together_copy_split_by_orca_is_rearranged_whole(tmp_path):
    # 150 mm bed: the fake arranger splits the second bundle copy across plates
    m = _check(_post(tmp_path, bed_x="150", bed_y="150", bed_z="250"), tmp_path, 150)
    assert len(m["plates"]) > 3


def test_machine_uuid_bed(tmp_path):
    _check(_post(tmp_path, machine_uuid="m350"), tmp_path, 350)


def test_part_larger_than_bed_is_part_too_large(tmp_path):
    r = _post(tmp_path, bed_x="45", bed_y="45", bed_z="250")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "part_too_large"
    # b.stl (40) and the 30 mm cube fit; the 70 mm bundle parts and 50 mm solo STL do not
    assert {d["loc"][2] for d in err["details"]} == {2, 3}


def test_together_copy_larger_than_one_plate_is_part_too_large(tmp_path):
    # each bundle part (70 x 60) fits a 100 mm bed, the two together never do
    r = _post(tmp_path, sel=[SEL[2]], bed_x="100", bed_y="100", bed_z="250")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "part_too_large"
    assert [d["loc"][2] for d in err["details"]] == [0]


@pytest.mark.parametrize("sel,data", [
    (SEL, {"machine_uuid": "m350", "bed_x": "256", "bed_y": "256", "bed_z": "256"}),
    (SEL, {"bed_x": "256", "bed_y": "256"}),
    (SEL, {"bed_x": "256", "bed_y": "0", "bed_z": "256"}),
    (SEL, {"machine_uuid": "nope"}),
    ([{"file": "a.3mf", "id": 2, "qty": 1, "rule": "glued"}], {}),
    ([{"file": "b.stl", "id": 2, "qty": 1}], {}),
    ([{"file": "a.3mf", "id": 99, "qty": 1}], {}),
    ([{"file": "a.3mf", "plate": 1, "qty": 300}], {}),             # 2-part bundle x 300 > cap
])
def test_other_errors_are_unprocessable(tmp_path, sel, data):
    r = _post(tmp_path, sel=sel, **data)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unprocessable"


def test_compose_inverts_to_local():
    tfs = {("2", 0): "1 0 0 0 1 0 0 0 1 10 20 0", ("2", 1): "1 0 0 0 1 0 0 0 1 317.2 20 0",
           ("4", 0): "1 0 0 0 1 0 0 0 1 10 -287.2 0"}
    plates = [[("2", 0)], [("2", 1)], [("4", 0)]]          # 3 plates: 2 x 2 grid on a 256 bed
    local = pm.to_local(tfs, plates, 256, 256)
    assert [float(v) for pl in local for _, t in pl for v in t.split()[9:11]] == pytest.approx([10, 20] * 3)
    back, lists = pm.compose(local, 256, 256)
    assert lists == plates
    for k, t in tfs.items():
        assert [float(v) for v in back[k].split()] == pytest.approx([float(v) for v in t.split()])


def test_renumber_follows_subset_numbering():
    assert pm.renumber({("2", 3), ("2", 1), ("4", 0)}) == {("2", 0): ("2", 1), ("2", 1): ("2", 3),
                                                           ("4", 0): ("4", 0)}


def test_pack_mixed_feature_flag():
    assert "pack.mixed" in TestClient(app).get("/api/v1").json()["features"]
