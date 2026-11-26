"""BIZ-407 live check against a running Laminus with the real OrcaSlicer.

Every slice must carry unique per-object labels in the G-code, in the .gcode.3mf plate
metadata, and in the neutral `objects` manifest on GET /api/v1/jobs/{id} - whatever the
preset says about label objects.

Skipped unless LAMINUS_URL is set (see test_export_3mf_live.py).
"""
import io
import json
import os
import re
import time
import zipfile

import httpx
import pytest

BASE = os.environ.get("LAMINUS_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="LAMINUS_URL not set (needs a live Laminus + OrcaSlicer)")

NS = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
BED = 220
STRIDE = BED * 1.2  # plate N sits N-1 strides to the right in the source project
BOXES = {"bracket": (20, 20, 10), "clip": (30, 15, 8)}


def _mesh(w, d, h):
    vs = [(x, y, z) for z in (0, h) for y in (-d / 2, d / 2) for x in (-w / 2, w / 2)]
    tris = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
            (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    return ("<mesh><vertices>" + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vs)
            + "</vertices><triangles>"
            + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
            + "</triangles></mesh>")


def make_project(plates, **settings):
    """plates: [[(model, x, y), ...], ...]. One object per entry, object name = model name."""
    cfg = {"printable_area": ["0x0", f"{BED}x0", f"{BED}x{BED}", f"0x{BED}"], "printable_height": "250",
           "layer_change_gcode": "G92 E0", "gcode_flavor": "klipper",
           "skirt_loops": "0", "brim_type": "no_brim"}
    cfg.update(settings)
    res, items, objs, pls, oid = [], [], [], [], 0
    for pno, members in enumerate(plates, start=1):
        mis = []
        for model, x, y in members:
            oid += 2
            res.append(f'<object id="{oid}" type="model">{_mesh(*BOXES[model])}</object>')
            items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x + (pno - 1) * STRIDE} {y} 0" printable="1"/>')
            objs.append(f'<object id="{oid}"><metadata key="name" value="{model}"/><part id="{oid}" subtype="normal_part"/></object>')
            mis.append(f'<model_instance><metadata key="object_id" value="{oid}"/><metadata key="instance_id" value="0"/></model_instance>')
        pls.append(f'<plate><metadata key="plater_id" value="{pno}"/>' + "".join(mis) + "</plate>")
    model = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model {NS}><resources>' + "".join(res)
             + "</resources><build>" + "".join(items) + "</build></model>")
    ms = '<?xml version="1.0" encoding="UTF-8"?>\n<config>' + "".join(objs) + "".join(pls) + "</config>"
    buf = io.BytesIO()
    from app.stl_to_3mf import _CONTENT_TYPES, _RELS
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", ms)
        z.writestr("Metadata/project_settings.config", json.dumps(cfg))
    return buf.getvalue()


TWO_MODELS = [[("bracket", 80, 110), ("clip", 150, 110)]]
TWO_COPIES = [[("bracket", 80, 110), ("bracket", 150, 110)]]
MULTI_PLATE = [[("bracket", 80, 110), ("clip", 150, 110)],
               [("bracket", 60, 110), ("bracket", 110, 110), ("bracket", 160, 110)]]


def slice_job(project, plate=1, export_3mf=None):
    r = httpx.post(f"{BASE}/api/v1/slice/prepared", timeout=60,
                   files={"file": ("project.3mf", project)},
                   data={"plate": str(plate), **({"export_3mf": export_3mf} if export_3mf else {})})
    assert r.status_code in (200, 202), r.text
    job_id = r.json().get("job_id") or r.json().get("id")
    deadline = time.monotonic() + 300
    while True:
        job = httpx.get(f"{BASE}/api/v1/jobs/{job_id}", timeout=30).json()
        if job["status"] in ("completed", "failed") or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert job["status"] == "completed", job
    art = httpx.get(f"{BASE}/api/v1/jobs/{job_id}/artifact", timeout=120)
    assert art.status_code == 200
    job = httpx.get(f"{BASE}/api/v1/jobs/{job_id}", timeout=30).json()  # still has its manifest
    return job, art.content


def gcode_of(content, export_3mf):
    if export_3mf:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            return z.read("Metadata/plate_1.gcode").decode()
    return content.decode()


# ---- independent G-code reading (does not use app code)
def klipper_defines(gcode):
    out = {}
    for m in re.finditer(r"^EXCLUDE_OBJECT_DEFINE NAME=(\S+) CENTER=(\S+) POLYGON=(\[.*\])$", gcode, re.M):
        out[m.group(1)] = (m.group(2), json.loads(m.group(3)))
    return out


def printed_extent(gcode, label):
    """XY bbox of extrusion moves between EXCLUDE_OBJECT_START/END for *label*."""
    xs, ys, on, x, y = [], [], False, None, None
    for line in gcode.splitlines():
        if line.startswith("EXCLUDE_OBJECT_START"):
            on = line.endswith("NAME=" + label)
        elif line.startswith("EXCLUDE_OBJECT_END"):
            on = False
        elif line.startswith("G1 "):
            mx, my = re.search(r"X(-?[\d.]+)", line), re.search(r"Y(-?[\d.]+)", line)
            x = float(mx.group(1)) if mx else x
            y = float(my.group(1)) if my else y
            if on and re.search(r"E\d|E\.", line) and x is not None and y is not None:
                xs.append(x)
                ys.append(y)
    assert xs, f"{label} has no extrusion"
    return min(xs), min(ys), max(xs), max(ys)


def objects_of(job):
    plates = job["objects"]["plates"]
    assert len(plates) == 1
    return plates[0]


def assert_labels(job, content, export_3mf, expect_count, flavor="klipper"):
    plate = objects_of(job)
    objs = plate["objects"]
    labels = [o["label"] for o in objs]
    assert len(labels) == expect_count
    assert all(labels) and len(set(labels)) == len(labels), labels
    # bed
    assert plate["bed"] == {"origin": [0.0, 0.0], "size": [float(BED), float(BED)]}
    gcode = gcode_of(content, export_3mf)
    if flavor == "klipper":
        defs = klipper_defines(gcode)
        assert set(defs) == set(labels)          # G-code defines exactly the manifest's labels
        for o in objs:
            center, poly = defs[o["label"]]
            assert re.fullmatch(r"-?[\d.]+,-?[\d.]+", center)
            assert o["polygon"] == poly
            x0, y0, x1, y1 = o["bbox"]
            assert 0 <= x0 < x1 <= BED and 0 <= y0 < y1 <= BED                       # inside the bed
            assert all(0 <= px <= BED and 0 <= py <= BED for px, py in o["polygon"])
            e = printed_extent(gcode, o["label"])                                       # inside what was printed
            assert e[0] - 0.5 <= x0 and e[1] - 0.5 <= y0 and x1 <= e[2] + 0.5 and y1 <= e[3] + 0.5
    else:
        assert set(re.findall(r"^M486 A(\S+)$", gcode, re.M)) == set(labels)
    if export_3mf:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            pj = json.loads(z.read("Metadata/plate_1.json"))
            info = z.read("Metadata/slice_info.config").decode()
        names = [e["name"] for e in pj["bbox_objects"]]
        assert sorted(names) == sorted(labels)      # plate metadata names == G-code labels
        for e in pj["bbox_objects"]:
            mine = next(o for o in objs if o["label"] == e["name"])
            assert all(abs(a - b) < 0.6 for a, b in zip(e["bbox"], mine["bbox"]))  # same footprint
        assert sorted(re.findall(r'<object identify_id="\d+" name="([^"]+)"', info)) == sorted(labels)
        assert 'key="label_object_enabled" value="true"' in info


@pytest.mark.parametrize("export_3mf", [None, "out.3mf"])
@pytest.mark.parametrize("plates,count", [(TWO_MODELS, 2), (TWO_COPIES, 2)])
def test_labels_unique_for_models_and_copies(plates, count, export_3mf):
    job, content = slice_job(make_project(plates), 1, export_3mf)
    assert_labels(job, content, export_3mf, count)


@pytest.mark.parametrize("plate,count", [(1, 2), (2, 3)])
def test_labels_unique_on_each_plate_of_a_multi_plate_project(plate, count):
    job, content = slice_job(make_project(MULTI_PLATE), plate, "out.3mf")
    assert objects_of(job)["plate"] == plate   # the caller's plate number, not Orca's internal 1
    assert_labels(job, content, "out.3mf", count)


def test_marlin_flavor_labels_too():
    job, content = slice_job(make_project(TWO_COPIES, gcode_flavor="marlin2"), 1, "out.3mf")
    assert_labels(job, content, "out.3mf", 2, flavor="marlin2")


def test_preset_with_label_objects_off_still_labels():
    off = make_project(TWO_COPIES, gcode_label_objects="0", exclude_object="0")
    job, content = slice_job(off, 1, "out.3mf")
    assert_labels(job, content, "out.3mf", 2)
    # and the same preset without the override really does not label (guards the premise)
    # - checked by the unit test on prepare_project; Orca itself honours the keys.
