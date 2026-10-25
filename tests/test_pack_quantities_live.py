"""Live /api/v1/pack `quantities` tests against a running Laminus (real OrcaSlicer).

Skipped unless LAMINUS_URL is set, e.g.:
    docker compose up -d && LAMINUS_URL=http://localhost:5000 pytest tests/test_pack_quantities_live.py
"""
import io
import json
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

import pytest
import httpx

BASE = os.environ.get("LAMINUS_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="LAMINUS_URL not set (needs a live Laminus + OrcaSlicer)")

_CUBE_TRIS = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
              (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
_PNS = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"


def cube_stl(size: float) -> bytes:
    verts = [(x, y, z) for x in (0, size) for y in (0, size) for z in (0, size)]
    buf = io.BytesIO()
    buf.write(b"\0" * 80)
    buf.write(struct.pack("<I", len(_CUBE_TRIS)))
    for tri in _CUBE_TRIS:
        buf.write(struct.pack("<3f", 0, 0, 0))
        for i in tri:
            buf.write(struct.pack("<3f", *verts[i]))
        buf.write(b"\0\0")
    return buf.getvalue()


def pack(stls, qty=None, bed=(100, 100, 100), timeout=150, path="/api/v1/pack", items=None):
    """stls: list of (filename, bytes); qty: copy counts aligned with stls (sent as `items`).

    Pass `items` (raw str) to send an arbitrary manifest. Returns the httpx.Response.
    """
    files = [("files", (n, b, "application/octet-stream")) for n, b in stls]
    data = {"bed_x": bed[0], "bed_y": bed[1], "bed_z": bed[2]}
    if items is None and qty is not None:
        items = json.dumps([{"file": n, "quantity": q} for (n, _), q in zip(stls, qty)])
    if items is not None:
        data["items"] = items
    return httpx.post(f"{BASE}{path}", files=files, data=data, timeout=timeout)


def analyse(content: bytes):
    """Return (instances_by_edge_size, n_plates) from a packed 3MF.

    Edge size is read from the referenced mesh's vertex extent, so instances are
    identified by geometry rather than by Orca's object naming.
    """
    z = zipfile.ZipFile(io.BytesIO(content))
    root = ET.fromstring(z.read("3D/3dmodel.model"))
    meshes = {}  # path -> edge size

    def mesh_edge(path):
        if path not in meshes:
            obj = ET.fromstring(z.read(path.lstrip("/")))
            xs = [float(v.get("x")) for v in obj.iter("{%s}vertex" % _NS["m"])]
            meshes[path] = round(max(xs) - min(xs))
        return meshes[path]

    objects = {}
    for o in root.find("m:resources", _NS).findall("m:object", _NS):
        comp = o.find("m:components/m:component", _NS)
        if comp is not None:
            objects[o.get("id")] = mesh_edge(comp.get(_PNS + "path"))
        else:
            xs = [float(v.get("x")) for v in o.iter("{%s}vertex" % _NS["m"])]
            objects[o.get("id")] = round(max(xs) - min(xs))

    counts: dict[int, int] = {}
    for item in root.find("m:build", _NS).findall("m:item", _NS):
        edge = objects[item.get("objectid")]
        counts[edge] = counts.get(edge, 0) + 1

    settings = z.read("Metadata/model_settings.config").decode()
    n_plates = len(re.findall(r"<plate>", settings))
    return counts, n_plates


@pytest.fixture(scope="module", autouse=True)
def _live():
    try:
        r = httpx.get(f"{BASE}/api/v1/health", timeout=5)
        assert r.json().get("orcaslicer_installed")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Laminus not reachable at {BASE}: {e}")


A, B = ("a.stl", cube_stl(40)), ("b.stl", cube_stl(20))


# ---- positive ---------------------------------------------------------------

def test_acceptance_3_of_a_and_1_of_b():
    r = pack([A, B], [3,1])
    assert r.status_code == 200, r.text
    counts, _ = analyse(r.content)
    assert counts == {40: 3, 20: 1}


def test_copies_spill_onto_multiple_plates():
    # 60x60 bed fits exactly one 40mm cube per plate.
    r = pack([A], [3], bed=(60, 60, 100))
    assert r.status_code == 200, r.text
    counts, plates = analyse(r.content)
    assert counts == {40: 3}
    assert plates == 3


def test_single_file_high_quantity():
    r = pack([B], [12])
    assert r.status_code == 200, r.text
    counts, _ = analyse(r.content)
    assert counts == {20: 12}


def test_all_ones_matches_legacy():
    r1 = pack([A, B], [1,1])
    r2 = pack([A, B])
    assert r1.status_code == r2.status_code == 200
    assert analyse(r1.content)[0] == analyse(r2.content)[0] == {40: 1, 20: 1}


def test_legacy_without_quantities_unchanged():
    r = pack([A, B])
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {40: 1, 20: 1}


def test_legacy_duplicate_filenames_still_two_instances():
    r = pack([A, A])
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {40: 2}


def test_total_at_cap_is_accepted():
    r = pack([B], [500], bed=(256, 256, 256), timeout=300)
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {20: 500}


# ---- negative ---------------------------------------------------------------

def _it(file, quantity):
    return {"file": file, "quantity": quantity}


BAD = {
    "not_json": "not json",
    "object_not_array": '{"a":1}',
    "scalar": "5",
    "empty_array": "[]",
    "missing_entry": [_it("a.stl", 1)],
    "unknown_file": [_it("a.stl", 1), _it("zzz.stl", 1)],
    "duplicate_entry": [_it("a.stl", 1), _it("a.stl", 1)],
    "entry_not_object": ["a.stl", "b.stl"],
    "missing_quantity": [_it("a.stl", 1), {"file": "b.stl"}],
    "zero": [_it("a.stl", 0), _it("b.stl", 1)],
    "negative": [_it("a.stl", -1), _it("b.stl", 1)],
    "float": [_it("a.stl", 1.5), _it("b.stl", 1)],
    "bool": [_it("a.stl", True), _it("b.stl", 1)],
    "string_qty": [_it("a.stl", "2"), _it("b.stl", 1)],
    "over_cap": [_it("a.stl", 501), _it("b.stl", 1)],
    "sum_over_cap": [_it("a.stl", 250), _it("b.stl", 251)],
}


@pytest.mark.parametrize("items", BAD.values(), ids=BAD.keys())
@pytest.mark.parametrize("path", ["/api/v1/pack", "/api/v1/pack"])
def test_bad_items_rejected_422(items, path):
    raw = items if isinstance(items, str) else json.dumps(items)
    r = pack([A, B], items=raw, path=path)
    assert r.status_code == 422, (items, r.status_code, r.text)


def test_duplicate_upload_filenames_rejected_when_items_given():
    r = pack([A, A], items=json.dumps([_it("a.stl", 1), _it("a.stl", 1)]))
    assert r.status_code == 422


def test_v1_error_details_point_at_each_problem():
    r = pack([A, B], items=json.dumps([_it("a.stl", 0), _it("zzz.stl", 2), _it("b.stl", 1.5)]),
             path="/api/v1/pack")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    locs = {tuple(d["loc"]): d["msg"] for d in err["details"]}
    assert locs[("body", "items", 0, "quantity")] == "must be >= 1"
    assert ("body", "items", 1, "file") in locs
    assert locs[("body", "items", 2, "quantity")] == "must be an integer"


def test_legacy_error_stays_plain_string():
    r = pack([A, B], items=json.dumps([_it("a.stl", 0), _it("b.stl", 1)]))
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_v1_pack_with_items_works():
    r = pack([A, B], [2, 1], path="/api/v1/pack")
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {40: 2, 20: 1}


def test_items_order_independent_of_upload_order():
    r = pack([A, B], items=json.dumps([_it("b.stl", 1), _it("a.stl", 3)]))
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {40: 3, 20: 1}


def test_too_many_unique_files_still_422():
    r = pack([(f"p{i}.stl", cube_stl(10)) for i in range(51)])
    assert r.status_code == 422


def test_rejection_does_not_leak_work_dirs():
    before = httpx.get(f"{BASE}/api/v1/health", timeout=5).json().get("active_jobs")
    pack([A, B], [0,1])
    assert httpx.get(f"{BASE}/api/v1/health", timeout=5).json().get("active_jobs") == before
