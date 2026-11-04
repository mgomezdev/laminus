"""BIZ-363: POST /api/v1/projects/measure - object / plate / STL bounding-box sizes."""
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import subset_3mf as s
from app.main import app
from tests.test_pack_next import _box_stl
from tests.test_plate_isolation import _bed, _project


def _three_plate(path):
    # plate 1 (centres): 70x60x10 at x=20 + 30x30x30 at x=120 -> 150 wide; plate 2: 40x20x5; plate 3: 50x50x50
    _project(path, [[("4", (70, 60, 10), 20, 20), ("2", (30, 30, 30), 120, 20)],
                    [("6", (40, 20, 5), 330, 20)],
                    [("8", (50, 50, 50), 20, -290)]], _bed(256, 256, 256))


def _rotate_first_item(path, oid):
    """Turn object *oid*'s first build item 90 degrees about Z, in place."""
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    model = files[s.MODEL].decode()

    def rot(m):
        t = m.group(2).split()
        return m.group(1) + " ".join(["0", "1", "0", "-1", "0", "0", "0", "0", "1"] + t[9:]) + '"'
    files[s.MODEL] = re.sub(rf'(<item objectid="{oid}"[^>]*?transform=")([^"]*)"', rot, model, count=1).encode()
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)


def _post(name, data, selection=None):
    form = {} if selection is None else {"selection": json.dumps(selection)}
    return TestClient(app).post("/api/v1/projects/measure", data=form,
                                files={"file": (name, data, "application/octet-stream")})


def _sizes(r):
    assert r.status_code == 200, r.text
    return [i["size_mm"] for i in r.json()["items"]]


def test_objects_and_plates(tmp_path):
    p = tmp_path / "a.3mf"
    _three_plate(p)
    got = _sizes(_post("a.3mf", p.read_bytes(), [{"id": 4}, {"id": 2}, {"plate": 1}, {"plate": 2}, {"plate": 3}]))
    assert got == [pytest.approx(v) for v in ([70, 60, 10], [30, 30, 30], [150, 60, 30], [40, 20, 5], [50, 50, 50])]


def test_rotated_object_reports_its_placed_size(tmp_path):
    p = tmp_path / "a.3mf"
    _three_plate(p)
    _rotate_first_item(p, "4")
    assert _sizes(_post("a.3mf", p.read_bytes(), [{"id": 4}])) == [pytest.approx([60, 70, 10])]


def test_object_by_name(tmp_path):
    p = tmp_path / "a.3mf"
    _three_plate(p)
    names = {o["id"]: o["name"] for o in s.list_objects(str(p))}
    assert _sizes(_post("a.3mf", p.read_bytes(), [{"name": names[6]}])) == [pytest.approx([40, 20, 5])]


def test_whole_single_plate_project(tmp_path):
    p = tmp_path / "a.3mf"
    _project(p, [[("4", (70, 60, 10), 20, 20), ("2", (30, 30, 30), 120, 20)]], _bed(256, 256, 256))
    for sel in (None, []):
        assert _sizes(_post("a.3mf", p.read_bytes(), sel)) == [pytest.approx([150, 60, 30])]


def test_stl_extents():
    assert _sizes(_post("b.stl", _box_stl(40, 30, 20))) == [pytest.approx([40, 30, 20])]


@pytest.mark.parametrize("name,sel", [
    ("a.3mf", None),                       # three plates: whole-file size is meaningless
    ("a.3mf", [{"id": 99}]),
    ("a.3mf", [{"plate": 9}]),
    ("a.3mf", [{"id": 4, "plate": 1}]),
    ("a.3mf", [{"qty": 2}]),
    ("a.3mf", [{"id": "4"}]),
    ("a.3mf", {"id": 4}),                  # not a list
    ("b.stl", [{"id": 2}]),
    ("junk.3mf", None),
    ("a.obj", None),
])
def test_errors_are_unprocessable(tmp_path, name, sel):
    p = tmp_path / "a.3mf"
    _three_plate(p)
    data = {"a.3mf": p.read_bytes(), "b.stl": _box_stl(10, 10, 10)}.get(name, b"not a zip")
    r = _post(name, data, sel)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "unprocessable"


def test_measure_feature_flag():
    assert "projects.measure" in TestClient(app).get("/api/v1").json()["features"]
