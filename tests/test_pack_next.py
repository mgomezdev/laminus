"""BIZ-361: POST /api/v1/pack/next - the fullest single plate a machine can print."""
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
from app import pack_next as pn
from app import plate_extract as pe
from app import subset_3mf as s
from app.main import app
from tests.test_plate_isolation import _bed, _project


def _box_stl(w, d, h) -> bytes:
    vs = [(x, y, z) for z in (0, h) for y in (0, d) for x in (0, w)]
    tris = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
            (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    buf = io.BytesIO()
    buf.write(b"\0" * 80)
    buf.write(struct.pack("<I", len(tris)))
    for t in tris:
        buf.write(struct.pack("<3f", 0, 0, 0))
        for i in t:
            buf.write(struct.pack("<3f", *vs[i]))
        buf.write(b"\0\0")
    return buf.getvalue()


def _machine(name, w, h=250):
    return {"name": name, "nozzle_diameter": ["0.4"], **_bed(w, w, h)}


class _Catalog:
    cfgs = {"m180": _machine("Small 180", 180), "m350": _machine("Big 350", 350)}

    def get_by_uuid(self, u):
        return {"uuid": u, "type": "machine", "name": self.cfgs[u]["name"]} if u in self.cfgs else None

    def resolved(self, u):
        return self.cfgs[u]


GAP = 2.0


def fake_arrange(oracle_path, out_path):
    """Stand-in for `orcaslicer --arrange`: shelf-pack build items in build order onto plates
    of the project's bed; an item larger than the bed gets a plate of its own."""
    with zipfile.ZipFile(oracle_path) as z:
        files = {n: z.read(n) for n in z.namelist()}
        boxes = pe.item_boxes(z)
    cfg = json.loads(files[s.PROJECT_SETTINGS])
    x0, y0, x1, y1 = pe.bed_rect(cfg)
    W, D = x1 - x0, y1 - y0
    model = ET.fromstring(files[s.MODEL])
    ms = ET.fromstring(files[s.MODEL_SETTINGS])
    items = model.find(s._q("build")).findall(s._q("item"))
    plates, cur, x, y, row = [], [], 0.0, 0.0, 0.0
    placed = {}
    for it, (oid, lo, hi) in zip(items, boxes):
        w, d = hi[0] - lo[0], hi[1] - lo[1]
        if w > W or d > D:
            plates.append([oid])
            placed[oid] = (len(plates) - 1, 0.0, 0.0, lo)
            continue
        if x + w > W:
            x, y, row = 0.0, y + row + GAP, 0.0
        if y + d > D:
            plates.append(cur)
            cur, x, y, row = [], 0.0, 0.0, 0.0
        cur.append(oid)
        placed[oid] = (None, x, y, lo)
        x, row = x + w + GAP, max(row, d)
    if cur:
        plates.append(cur)
    plate_of = {oid: n for n, ps in enumerate(plates) for oid in ps}
    cols = 1 if len(plates) == 1 else 2
    for it in items:
        oid = it.get("objectid")
        _, px, py, lo = placed[oid]
        n = plate_of[oid]
        ox, oy = (n % cols) * W * 1.2, -(n // cols) * D * 1.2
        t = it.get("transform").split()
        t[9] = str(float(t[9]) - lo[0] + x0 + ox + px)
        t[10] = str(float(t[10]) - lo[1] + y0 + oy + py)
        it.set("transform", " ".join(t))
    tmpl = ms.find("plate")
    for pl in ms.findall("plate"):
        ms.remove(pl)
    for n, ps in enumerate(plates):
        pl = ET.SubElement(ms, "plate")
        ET.SubElement(pl, "metadata", {"key": "plater_id", "value": str(n + 1)})
        for oid in ps:
            mi = ET.SubElement(pl, "model_instance")
            ET.SubElement(mi, "metadata", {"key": "object_id", "value": oid})
            ET.SubElement(mi, "metadata", {"key": "instance_id", "value": "0"})
    assert tmpl is not None
    files[s.MODEL] = ET.tostring(model)
    files[s.MODEL_SETTINGS] = ET.tostring(ms)
    with zipfile.ZipFile(out_path, "w") as z:
        for n, data in files.items():
            z.writestr(n, data)


async def _fake_orca(*args, **kwargs):
    a = list(args)
    fake_arrange(a[-1], a[a.index("--export-3mf") + 1])
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"ok", None))
    return proc


def _bundle_3mf(path):
    # plate 1: a 70x60 + 70x60 bundle (together); plate 2: a 30 mm cube
    _project(path, [[("4", (70, 60, 10), 60, 60), ("6", (70, 60, 10), 140, 60)],
                    [("2", (30, 30, 30), 300, 60)]], _bed(256, 256, 256))


SEL = [{"file": "a.3mf", "plate": 1, "qty": 2, "rule": "together"},
       {"file": "a.3mf", "id": 2, "qty": 3},
       {"file": "b.stl", "qty": 6}]


def _post(tmp_path, machine, sel=SEL, **data):
    a = tmp_path / "a.3mf"
    _bundle_3mf(a)
    files = [("files", ("a.3mf", a.read_bytes(), "application/octet-stream")),
             ("files", ("b.stl", _box_stl(40, 40, 20), "application/octet-stream"))]
    with patch("app.main.catalog", _Catalog()), patch("asyncio.create_subprocess_exec", new=_fake_orca):
        return TestClient(app).post("/api/v1/pack/next", files=files,
                                    data={"selection": json.dumps(sel), "machine_uuid": machine, **data})


def _plates(content):
    ms = ET.fromstring(zipfile.ZipFile(io.BytesIO(content)).read(s.MODEL_SETTINGS))
    return [Counter(s._meta(mi, "object_id") for mi in pl.findall("model_instance"))
            for pl in ms.findall("plate")]


def _check_plate(r, bed_w):
    assert r.status_code == 200, r.text
    content = r.content
    assert len(_plates(content)) == 1
    m = json.loads(zipfile.ZipFile(io.BytesIO(content)).read(mf.MANIFEST))
    taken = json.loads(r.headers["X-Consumed"])
    assert [i["consumed"] for i in m["items"]] == taken
    assert [i["requested"] for i in m["items"]] == [4, 3, 6]
    insts = m["plates"][0]["instances"]
    assert Counter(i["item"] for i in insts) == {i: n for i, n in enumerate(taken) if n}
    assert Counter(i["object_id"] for i in insts) == _plates(content)[0]
    # together copies are whole: every copy brings both bundle parts
    bundle = Counter(i["part"] for i in insts if i["item"] == 0)
    assert bundle["4"] == bundle["6"]
    # on the bed and centred
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        x0, y0, x1, y1, _ = pe.plate_footprint(z)
    assert x0 >= -0.01 and y0 >= -0.01 and x1 <= bed_w + 0.01 and y1 <= bed_w + 0.01
    assert (x0 + x1) / 2 == pytest.approx(bed_w / 2) and (y0 + y1) / 2 == pytest.approx(bed_w / 2)
    return taken


def test_one_plate_per_machine_and_bigger_bed_takes_more(tmp_path):
    small = _check_plate(_post(tmp_path, "m180"), 180)
    big = _check_plate(_post(tmp_path, "m350"), 350)
    assert 0 < sum(small) <= sum(big)
    assert sum(big) == 4 + 3 + 6       # everything fits the 350 bed
    assert sum(small) < sum(big)


def test_together_copy_split_by_orca_is_dropped_whole(tmp_path):
    # 150 mm bed: one bundle copy (142 wide) per shelf row; the second copy's parts
    # straddle rows/plates in the fake arranger unless placed together
    _Catalog.cfgs["m150"] = _machine("Mid 150", 150)
    r = _post(tmp_path, "m150", sel=[SEL[0]])
    assert r.status_code == 200, r.text
    taken = json.loads(r.headers["X-Consumed"])
    assert taken[0] % 2 == 0 and taken[0] >= 2


def test_max_parts_trims_whole_units(tmp_path):
    r = _post(tmp_path, "m350", max_parts="5")
    taken = json.loads(r.headers["X-Consumed"])
    assert r.status_code == 200 and sum(taken) <= 5
    assert taken[0] % 2 == 0           # never half a bundle


def test_nothing_fits_is_its_own_code(tmp_path):
    _Catalog.cfgs["m25"] = _machine("Tiny 25", 25)
    r = _post(tmp_path, "m25")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "nothing_fits"
    assert {d["loc"][2] for d in err["details"]} == {0, 1, 2}


@pytest.mark.parametrize("sel,data", [
    ([{"file": "a.3mf", "id": 2, "qty": 1, "rule": "solo"}], {}),
    ([{"file": "a.3mf", "id": 2, "qty": 1, "rule": "glued"}], {}),
    ([{"file": "a.3mf", "id": 99, "qty": 1}], {}),
    ([{"file": "b.stl", "id": 2, "qty": 1}], {}),
    ([{"file": "zzz.3mf", "id": 2}], {}),
    ([{"file": "a.3mf", "id": 2, "qty": 0}], {}),
    (SEL, {"max_parts": "0"}),
])
def test_other_errors_never_use_nothing_fits(tmp_path, sel, data):
    r = _post(tmp_path, "m350", sel=sel, **data)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] != "nothing_fits"


def test_unknown_machine_is_not_nothing_fits(tmp_path):
    r = _post(tmp_path, "nope")
    assert r.status_code == 422 and r.json()["error"]["code"] != "nothing_fits"


def test_choose_keeps_whole_units_in_plate_order():
    origins = {"2": [{"item": 0, "unit": ("u", 0)}, {"item": 0, "unit": ("u", 0)}],
               "4": [{"item": 1, "unit": (1, "4", 0)}, {"item": 1, "unit": (1, "4", 1)}]}
    p1 = [("4", 0), ("2", 0), ("4", 1)]               # bundle half on plate 1 -> dropped
    assert pn.choose(p1, origins, None) == [("4", 0), ("4", 1)]
    # max 3: the bundle (placed before ("4", 1)) fits after ("4", 0); ("4", 1) is trimmed
    assert pn.choose(p1 + [("2", 1)], origins, 3) == [("4", 0), ("2", 0), ("2", 1)]
    assert pn.choose(p1 + [("2", 1)], origins, 2) == [("4", 0), ("4", 1)]
    assert pn.choose(p1 + [("2", 1)], origins, None) == [("4", 0), ("2", 0), ("2", 1), ("4", 1)]


def test_pack_next_feature_flag():
    assert "pack.next" in TestClient(app).get("/api/v1").json()["features"]
