"""BIZ-359: Metadata/laminus_manifest.json in /pack, /projects/subset and /projects/merge output.

The manifest must list, per plate, exactly the build instances on that plate (as written in
model_settings.config) with the request item / part each came from, and per-item totals
must equal what was requested.
"""
import io
import json
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import manifest as mf
from app import subset_3mf as s
from app.main import app
from app.plate_extract import extract_plate
from tests.conftest import fake_pack_export
from tests.test_subset_3mf import make_3mf


def _stl(tag: bytes) -> bytes:
    buf = io.BytesIO()
    buf.write(tag.ljust(80))
    buf.write(struct.pack("<I", 1))
    for f in (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0):
        buf.write(struct.pack("<f", f))
    buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def _proc():
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"ok", None))
    return proc


def _zip(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content))


def _actual_plates(content: bytes) -> dict[int, Counter]:
    """{plater_id: Counter(object_id)} straight from model_settings.config."""
    ms = ET.fromstring(_zip(content).read("Metadata/model_settings.config"))
    out = {}
    for n, pl in enumerate(ms.findall("plate"), start=1):
        pid = int(s._meta(pl, "plater_id") or n)
        out[pid] = Counter(s._meta(mi, "object_id") for mi in pl.findall("model_instance"))
    return out


def _manifest(content: bytes) -> dict:
    return json.loads(_zip(content).read(mf.MANIFEST))


def _assert_matches_plates(content: bytes) -> dict:
    m = _manifest(content)
    assert m["version"] == 1
    assert {p["plate"]: Counter(i["object_id"] for i in p["instances"]) for p in m["plates"]} \
        == _actual_plates(content)
    totals = Counter(i["item"] for p in m["plates"] for i in p["instances"])
    assert totals == {r["item"]: r["requested"] for r in m["items"]}
    return m


# ------------------------------------------------------------------ /pack

# uploads a, b, c with qty 5 / 2 / 7, listed in `items` as c, a, b
UPLOADS = ["a.stl", "b.stl", "c.stl"]
QTY = {"a.stl": 5, "b.stl": 2, "c.stl": 7}
ITEMS = [{"file": "c.stl", "quantity": 7}, {"file": "a.stl", "quantity": 5}, {"file": "b.stl", "quantity": 2}]
# Orca input positions: a = 0..4, b = 5..6, c = 7..13. Small bed -> 4 plates, mixed.
PLATES = [[13, 0, 5, 7], [1, 2, 8, 9], [6, 10, 11, 3], [4, 12]]


def _pack(plates=PLATES):
    async def _fake_orca(*args, **kwargs):
        a = list(args)
        with open(a[a.index("--export-3mf") + 1], "wb") as fh:
            fh.write(fake_pack_export(14, plates))
        return _proc()

    with patch("asyncio.create_subprocess_exec", new=_fake_orca):
        return TestClient(app).post(
            "/api/v1/pack",
            files=[("files", (n, _stl(n.encode()), "application/octet-stream")) for n in UPLOADS],
            data={"bed_x": "60", "bed_y": "60", "bed_z": "60", "items": json.dumps(ITEMS)})


def test_pack_manifest_lists_each_plates_instances():
    r = _pack()
    assert r.status_code == 200, r.text
    m = _assert_matches_plates(r.content)
    assert m["items"] == [{"item": 0, "source": "c.stl", "requested": 7},
                          {"item": 1, "source": "a.stl", "requested": 5},
                          {"item": 2, "source": "b.stl", "requested": 2}]
    source_of_pos = ["a.stl"] * 5 + ["b.stl"] * 2 + ["c.stl"] * 7
    item_of = {"c.stl": 0, "a.stl": 1, "b.stl": 2}
    assert len(m["plates"]) == 4
    for plate, positions in zip(m["plates"], PLATES):
        # fake export: input position k -> object id 2(k+1)
        want = sorted((str(2 * (k + 1)), source_of_pos[k]) for k in positions)
        assert sorted((i["object_id"], i["source"]) for i in plate["instances"]) == want
        for i in plate["instances"]:
            assert i["part"] == i["source"] and i["item"] == item_of[i["source"]]
    assert len({frozenset(Counter(i["source"] for i in p["instances"]).items()) for p in m["plates"]}) > 1


def test_pack_without_items_uses_upload_order():
    async def _fake_orca(*args, **kwargs):
        a = list(args)
        with open(a[a.index("--export-3mf") + 1], "wb") as fh:
            fh.write(fake_pack_export(2, [[1], [0]]))
        return _proc()

    with patch("asyncio.create_subprocess_exec", new=_fake_orca):
        r = TestClient(app).post(
            "/api/v1/pack", files=[("files", (n, _stl(n.encode()), "application/octet-stream"))
                                   for n in ("x.stl", "y.stl")],
            data={"bed_x": "60", "bed_y": "60", "bed_z": "60"})
    m = _assert_matches_plates(r.content)
    assert [(p["plate"], [(i["item"], i["source"]) for i in p["instances"]]) for p in m["plates"]] \
        == [(1, [(1, "y.stl")]), (2, [(0, "x.stl")])]


def test_pack_fails_rather_than_return_a_wrong_manifest():
    r = _pack([[0, 1, 2], [3, 4]])  # Orca "lost" nine copies
    assert r.status_code == 500
    assert "manifest" in r.json()["error"]["message"]


def test_pack_plate_numbers_are_the_slice_plate_numbers(tmp_path):
    content = _pack().content
    path = tmp_path / "packed.3mf"
    path.write_bytes(content)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("Metadata/project_settings.config",
                   json.dumps({"printable_area": ["0x0", "60x0", "60x60", "0x60"]}))
    for plate in _manifest(content)["plates"]:
        info = extract_plate(str(path), plate["plate"], str(tmp_path / f"p{plate['plate']}.3mf"))
        assert Counter(info["objects"]) == Counter(sorted({i["object_id"] for i in plate["instances"]}))
        assert info["kept_items"] == len(plate["instances"])


# ------------------------------------------------------------------ subset / merge

def _fake_chunks(oracle_path, out_path, per_plate=2):
    """Stand-in for `orcaslicer --arrange`: instances in reverse order, `per_plate` per plate."""
    with zipfile.ZipFile(oracle_path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    ms = ET.fromstring(files[s.MODEL_SETTINGS])
    plate = ms.find("plate")
    insts = plate.findall("model_instance")[::-1]
    for mi in insts:
        plate.remove(mi)
    for n in range(0, len(insts), per_plate):
        pl = plate if n == 0 else ET.SubElement(ms, "plate")
        if n:
            ET.SubElement(pl, "metadata", {"key": "plater_id", "value": str(n // per_plate + 1)})
        for mi in insts[n:n + per_plate]:
            pl.append(mi)
    files[s.MODEL_SETTINGS] = ET.tostring(ms)
    with zipfile.ZipFile(out_path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)


async def _fake_orca(*args, **kwargs):
    a = list(args)
    _fake_chunks(a[-1], a[a.index("--export-3mf") + 1])
    return _proc()


def test_subset_manifest_attributes_instances_in_selection_order(tmp_path):
    src = tmp_path / "src.3mf"
    make_3mf(src)
    sel = [{"name": "Lid", "qty": 3}, {"plate": 1, "qty": 2}]   # Lid x3 + (Lid + Base) x2
    with patch("asyncio.create_subprocess_exec", new=_fake_orca):
        r = TestClient(app).post("/api/v1/projects/subset",
                                 files=[("file", ("model.3mf", src.read_bytes(), "application/octet-stream"))],
                                 data={"selection": json.dumps(sel)})
    assert r.status_code == 200, r.text
    m = _assert_matches_plates(r.content)
    assert m["items"] == [{"item": 0, "source": "model.3mf", "requested": 3},
                          {"item": 1, "source": "model.3mf", "requested": 4}]
    insts = [i for p in m["plates"] for i in p["instances"]]
    assert Counter((i["item"], i["part"]) for i in insts) == {(0, "2"): 3, (1, "2"): 2, (1, "4"): 2}
    assert {i["object_name"] for i in insts} == {"Lid", "Base"}
    assert len(m["plates"]) == 4


def test_merge_manifest_uses_global_selection_index_and_merged_ids(tmp_path):
    a, b = tmp_path / "a.3mf", tmp_path / "b.3mf"
    make_3mf(a)
    make_3mf(b)
    sel = [{"file": "first.3mf", "plate": 1, "qty": 2},    # 0: Lid x2 + Base x2 from first
           {"file": 1, "name": "Lid", "qty": 1},           # 1: Lid x1 from second
           {"file": 0, "id": 4, "qty": 1}]                 # 2: Base x1 from first
    files = [("files", ("first.3mf", a.read_bytes(), "application/octet-stream")),
             ("files", ("second.3mf", b.read_bytes(), "application/octet-stream"))]
    with patch("asyncio.create_subprocess_exec", new=_fake_orca):
        r = TestClient(app).post("/api/v1/projects/merge", files=files, data={"selection": json.dumps(sel)})
    assert r.status_code == 200, r.text
    m = _assert_matches_plates(r.content)
    assert m["items"] == [{"item": 0, "source": "first.3mf", "requested": 4},
                          {"item": 1, "source": "second.3mf", "requested": 1},
                          {"item": 2, "source": "first.3mf", "requested": 1}]
    insts = [i for p in m["plates"] for i in p["instances"]]
    assert Counter((i["item"], i["source"], i["part"]) for i in insts) == {
        (0, "first.3mf", "2"): 2, (0, "first.3mf", "4"): 2,
        (1, "second.3mf", "2"): 1, (2, "first.3mf", "4"): 1}
    # second.3mf's Lid was renumbered in the merge; the manifest names the output id
    lid2 = {i["object_id"] for i in insts if i["source"] == "second.3mf"}
    assert len(lid2) == 1 and lid2.isdisjoint({"2", "4"})
    assert {i["object_id"] for i in insts if i["source"] == "first.3mf"} == {"2", "4"}


def test_embed_replaces_an_existing_manifest(tmp_path):
    p = tmp_path / "x.3mf"
    p.write_bytes(fake_pack_export(1))
    mf.embed_manifest(str(p), {"version": 1, "old": True})
    mf.embed_manifest(str(p), {"version": 1, "new": True})
    with zipfile.ZipFile(p) as z:
        assert z.namelist().count(mf.MANIFEST) == 1
    assert mf.read_manifest(str(p)) == {"version": 1, "new": True}


def test_pack_manifest_feature_flag():
    assert "pack.manifest" in TestClient(app).get("/api/v1").json()["features"]
