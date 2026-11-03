"""BIZ-355: plate N of a multi-plate 3MF sliced on a bed that differs from the source bed.

The plate must be located with the SOURCE project's bed (the grid it was laid out on),
moved from the source bed centre to the target bed centre (layout preserved), checked
against the target bed, and only then given the target machine's settings.
"""
import itertools
import json
import re
import zipfile

import pytest

from app.plate_extract import PlateFitError, extract_plate, plate_footprint, retarget_plate
from tests.test_empty_plate_output import _run

NS = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
NS_P = 'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'


def _box_mesh(w, d, h):
    """Box centred on the origin in XY, z 0..h."""
    vs = [(x, y, z) for z in (0, h) for y in (-d / 2, d / 2) for x in (-w / 2, w / 2)]
    tris = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
            (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    return ("<mesh><vertices>" + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in vs)
            + "</vertices><triangles>"
            + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
            + "</triangles></mesh>")


def _bed(w, d, h=None, x0=0, y0=0):
    cfg = {"printable_area": [f"{x0:g}x{y0:g}", f"{x0 + w:g}x{y0:g}",
                              f"{x0 + w:g}x{y0 + d:g}", f"{x0:g}x{y0 + d:g}"]}
    if h is not None:
        cfg["printable_height"] = str(h)
    return cfg


def _project(path, plates, bed_cfg, modifier=None):
    """plates: list of lists of (object_id, (w, d, h), x, y). One object per build item.

    Object "2" (if used) lives in a production-extension sub-model, the rest are inline
    meshes. *modifier* = (object_id, (w, d, h)) adds a huge modifier part to that object.
    """
    items, ms_plates, objs, sub = [], [], {}, ""
    for pno, members in enumerate(plates, start=1):
        mis = []
        for oid, dims, x, y in members:
            objs[oid] = dims
            items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x} {y} 0" printable="1"/>')
            mis.append(f'<model_instance><metadata key="object_id" value="{oid}"/>'
                       f'<metadata key="instance_id" value="0"/></model_instance>')
        ms_plates.append(f'<plate><metadata key="plater_id" value="{pno}"/>' + "".join(mis) + "</plate>")
    res, ms_objs = [], []
    for oid, dims in sorted(objs.items()):
        parts = f'<part id="{oid}" subtype="normal_part"/>'
        if oid == "2":
            sub += f'<object id="102" type="model">{_box_mesh(*dims)}</object>'
            comps = '<component p:path="/3D/Objects/o2.model" objectid="102"/>'
            if modifier and modifier[0] == oid:
                sub += f'<object id="103" type="model">{_box_mesh(*modifier[1])}</object>'
                comps += '<component p:path="/3D/Objects/o2.model" objectid="103"/>'
                parts += '<part id="103" subtype="modifier_part"/>'
            res.append(f'<object id="{oid}" type="model"><components>{comps}</components></object>')
        else:
            res.append(f'<object id="{oid}" type="model">{_box_mesh(*dims)}</object>')
        ms_objs.append(f'<object id="{oid}"><metadata key="name" value="o{oid}"/>{parts}</object>')
    model = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model {NS} {NS_P}><resources>'
             + "".join(res) + "</resources><build>" + "".join(items) + "</build></model>")
    ms = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>' + "".join(ms_objs)
          + "".join(ms_plates) + "</config>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        if sub:
            z.writestr("3D/Objects/o2.model", f'<?xml version="1.0"?>\n<model {NS}><resources>{sub}</resources></model>')
        z.writestr("Metadata/model_settings.config", ms)
        z.writestr("Metadata/project_settings.config", json.dumps(bed_cfg))


def _positions(path):
    with zipfile.ZipFile(path) as z:
        model = z.read("3D/3dmodel.model").decode()
    return {m.group(1): tuple(float(v) for v in m.group(2).split()[9:11])
            for m in re.finditer(r'<item objectid="(\d+)" transform="([^"]+)"', model)}


def _settings(path):
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


def _centre(rect_cfg):
    xs = [float(p.split("x")[0]) for p in rect_cfg["printable_area"]]
    ys = [float(p.split("x")[1]) for p in rect_cfg["printable_area"]]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2


# Packed for the smallest eligible bed (256): two plates, stride 256 * 1.2 = 307.2 in x.
SRC_BED = _bed(256, 256, 256)
STRIDE = 256 * 1.2
PLATE2 = [("2", (30, 30, 30), 128, 128), ("4", (20, 20, 20), 60, 80), ("6", (10, 10, 50), 190, 170)]


def _multi(path, plate2=PLATE2, src_bed=SRC_BED, **kw):
    _project(path, [[("8", (40, 40, 10), 128, 128)],
                    [(o, dims, x + STRIDE, y) for o, dims, x, y in plate2]], src_bed, **kw)


def _slice(tmp_path, name, plate, target):
    src = tmp_path / f"{name}.3mf"
    _multi(src)
    work = tmp_path / name
    work.mkdir()
    return _run(work, src, plate, ["Metadata/plate_1.gcode"], target_settings=target)


@pytest.mark.parametrize("plate", [2])
def test_same_plate_identical_layout_on_256_and_350_beds(tmp_path, plate):
    beds = {"256": _bed(256, 256, 256), "350": _bed(350, 350, 350)}
    handed = {}
    for name, target in beds.items():
        job, args = _slice(tmp_path, name, plate, target)
        assert job["status"] == "completed", job.get("error")
        sliced = args[-1]
        assert args[args.index("--slice") + 1] == "1" and args[args.index("--arrange") + 1] == "0"
        assert _settings(sliced) == target  # target settings embedded after extraction
        handed[name] = _positions(sliced)

    # identical object set
    assert set(handed["256"]) == set(handed["350"]) == {"2", "4", "6"}
    # identical relative layout (pairwise offsets)
    for a, b in itertools.combinations(sorted(handed["256"]), 2):
        off =[tuple(p[b][i] - p[a][i] for i in range(2)) for p in handed.values()]
        assert off[0] == pytest.approx(off[1])
    # centred on each bed: the layout sits where it sat relative to the source bed centre
    for name, target in beds.items():
        cx, cy = _centre(target)
        rel = {o: (x - cx, y - cy) for o, (x, y) in handed[name].items()}
        assert rel == pytest.approx({o: (x - 128, y - 128) for o, _, x, y in PLATE2})


def test_plate_located_with_source_bed_not_target(tmp_path):
    # Pre-BIZ-355 the stride came from the target (350 -> 420 mm), dropping plate 2's
    # objects ~113 mm off the bed. Extraction must use the source project's 256 bed.
    src, out = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _multi(src)
    info = extract_plate(str(src), 2, str(out), _bed(350, 350, 350))
    assert info["origin_shift"] == pytest.approx([STRIDE, 0])
    assert info["source_bed"] == [0, 0, 256, 256]
    assert _positions(out)["2"] == pytest.approx((128, 128))


def test_source_without_printable_area_falls_back_to_target(tmp_path):
    src, out = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _multi(src, src_bed={})
    info = extract_plate(str(src), 2, str(out), _bed(256, 256))
    assert info["source_bed"] == [0, 0, 256, 256] and info["origin_shift"] == pytest.approx([STRIDE, 0])


def test_misfit_fails_job_with_readable_error(tmp_path):
    # footprint 300 x 210 x 120 on a 350 source bed, sliced on a 256^3 printer
    src = tmp_path / "in.3mf"
    big = [("2", (300, 210, 120), 175, 175)]
    _project(src, [[("8", (40, 40, 10), 175, 175)],
                   [(o, d, x + 350 * 1.2, y) for o, d, x, y in big]], _bed(350, 350, 350))
    job, args = _run(tmp_path, src, 2, ["Metadata/plate_1.gcode"], target_settings=_bed(256, 256, 256))
    assert job["status"] == "failed"
    assert job["error"] == ("Plate 2 footprint 300.0 × 210.0 × 120.0 mm does not fit "
                            "the target bed 256 × 256 × 256 mm")
    assert args is None  # Orca never ran


def test_too_tall_is_a_misfit(tmp_path):
    src, mid = tmp_path / "in.3mf", tmp_path / "mid.3mf"
    _project(src, [[("4", (20, 20, 300), 100, 100)]], _bed(256, 256, 256))
    info = extract_plate(str(src), 1, str(mid))
    with pytest.raises(PlateFitError, match=r"20.0 × 20.0 × 300.0 mm .* 256 × 256 × 256 mm"):
        retarget_plate(str(mid), str(tmp_path / "o.3mf"), 1, info["source_bed"], _bed(256, 256, 256))


def test_modifier_parts_do_not_count_toward_footprint(tmp_path):
    src = tmp_path / "in.3mf"
    _project(src, [[("2", (30, 30, 30), 128, 128)]], SRC_BED, modifier=("2", (500, 500, 500)))
    with zipfile.ZipFile(src) as z:
        assert plate_footprint(z) == pytest.approx((113, 113, 143, 143, 30))


def test_fits_but_offset_layout_centres_footprint(tmp_path):
    # 200 mm wide layout hugging the right edge of a 350 bed: fits a 256 bed only if centred
    src, mid, out = tmp_path / "in.3mf", tmp_path / "mid.3mf", tmp_path / "out.3mf"
    _project(src, [[("4", (200, 20, 10), 240, 175)]], _bed(350, 350, 350))
    info = extract_plate(str(src), 1, str(mid))
    placed = retarget_plate(str(mid), str(out), 1, info["source_bed"], _bed(256, 256, 256))
    assert placed["placement"] == "footprint_centre"
    assert _positions(out)["4"] == pytest.approx((128, 128))


def test_non_zero_bed_origin(tmp_path):
    # target bed spans -10..246: its centre is 118, so a centred source plate lands there
    src, mid, out = tmp_path / "in.3mf", tmp_path / "mid.3mf", tmp_path / "out.3mf"
    _project(src, [[("4", (20, 20, 10), 128, 100)]], SRC_BED)
    info = extract_plate(str(src), 1, str(mid))
    placed = retarget_plate(str(mid), str(out), 1, info["source_bed"], _bed(256, 256, x0=-10, y0=-10))
    assert placed["placement"] == "bed_centre"
    assert _positions(out)["4"] == pytest.approx((118, 90))


def test_plate_isolation_feature_flag():
    from fastapi.testclient import TestClient
    import app.main as main_mod
    assert "slice.plate_isolation" in TestClient(main_mod.app).get("/api/v1").json()["features"]
