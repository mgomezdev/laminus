"""BIZ-349: slicing plate N of a multi-plate 3MF.

Orca's `--arrange 1` re-packs objects across plates (plates collapse, `--slice 2` then
"succeeds" with nothing sliced) and `--arrange 0` can mis-judge plate bounds, so Laminus
extracts the requested plate into a single-plate project and slices that with
`--slice 1 --arrange 0`. A run that still yields no G-code must fail, never `completed`.
"""
import asyncio
import json
import re
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from app import main
from app.main import _sliced_plate_gcode_error, run_orcaslicer_task, JobLogger
from app.plate_extract import PlateExtractError, extract_plate, plate_origin

BED = (200.0, 100.0)  # W x D -> plate stride 240 in x, 120 in y


def _project(path, plates, bed=BED, extra_files=()):
    """plates: list of lists of (object_id, x, y) build items, one list per plate.

    Instances of the same object are numbered in build order, as Orca does.
    """
    items, seen, ms_plates, objects = [], {}, [], set()
    for pno, members in enumerate(plates, start=1):
        mis = []
        for oid, x, y in members:
            iid = seen.get(oid, 0)
            seen[oid] = iid + 1
            objects.add(oid)
            items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x} {y} 1" printable="1"/>')
            mis.append(f'<model_instance><metadata key="object_id" value="{oid}"/>'
                       f'<metadata key="instance_id" value="{iid}"/></model_instance>')
        ms_plates.append(f'<plate><metadata key="plater_id" value="{pno}"/>'
                         f'<metadata key="thumbnail_file" value="Metadata/plate_{pno}.png"/>'
                         + "".join(mis) + '</plate>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
             + "".join(f'<object id="{o}" type="model"/>' for o in sorted(objects))
             + '</resources><build>' + "".join(items) + '</build></model>')
    ms = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>'
          + "".join(f'<object id="{o}"><metadata key="name" value="o{o}"/></object>' for o in sorted(objects))
          + "".join(ms_plates) + '</config>')
    w, d = bed
    cfg = {"printable_area": ["0x0", f"{w:g}x0", f"{w:g}x{d:g}", f"0x{d:g}"]}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", ms)
        z.writestr("Metadata/project_settings.config", json.dumps(cfg))
        for n in range(1, len(plates) + 1):
            z.writestr(f"Metadata/plate_{n}.png", f"png{n}")
        for name, data in extra_files:
            z.writestr(name, data)


def _items(path):
    with zipfile.ZipFile(path) as z:
        model = z.read("3D/3dmodel.model").decode()
    return [(m.group(1), [float(v) for v in m.group(2).split()])
            for m in re.finditer(r'<item objectid="(\d+)" transform="([^"]+)"', model)]


def _plates(path):
    with zipfile.ZipFile(path) as z:
        ms = z.read("Metadata/model_settings.config").decode()
    return re.findall(r'plater_id" value="(\d+)"', ms)


# ------------------------------------------------------------------ plate_extract

def test_plate_origin_grid():
    # 3 plates -> 2 columns: plate 2 to the right, plate 3 below plate 1
    assert plate_origin(0, 3, 200, 100) == (0, 0)
    assert plate_origin(1, 3, 200, 100) == (240, 0)
    assert plate_origin(2, 3, 200, 100) == (0, -120)
    # 2 plates -> 2 columns (the pack fixture's 220 mm bed: stride 264)
    assert plate_origin(1, 2, 220, 220) == pytest.approx((264, 0))


def test_extract_later_plate_moves_it_onto_plate_one(tmp_path):
    src, dst = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _project(src, [[("2", 50, 50)], [("4", 290, 40), ("6", 320, 60)], [("8", 100, -70)]])
    info = extract_plate(str(src), 2, str(dst))
    assert info["source_plates"] == 3 and info["origin_shift"] == [240, 0]
    assert info["kept_items"] == 2 and info["dropped_items"] == 2
    assert [(o, t[9], t[10]) for o, t in _items(dst)] == [("4", 50, 40), ("6", 80, 60)]
    assert _plates(dst) == ["1"]
    with zipfile.ZipFile(dst) as z:
        names = z.namelist()
        assert z.read("Metadata/plate_1.png") == b"png2"  # this plate's thumbnail, renumbered
        assert "Metadata/plate_2.png" not in names and "Metadata/plate_3.png" not in names
        assert 'value="Metadata/plate_1.png"' in z.read("Metadata/model_settings.config").decode()


def test_extract_second_row(tmp_path):
    src, dst = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _project(src, [[("2", 50, 50)], [("4", 290, 40)], [("8", 100, -70)]])
    extract_plate(str(src), 3, str(dst))
    assert [(o, t[9], t[10]) for o, t in _items(dst)] == [("8", 100, 50)]


def test_extract_plate_one_still_drops_other_plates(tmp_path):
    # Plate 1 goes through extraction too: other plates' parts must not reach Orca.
    src, dst = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _project(src, [[("2", 50, 50)], [("4", 290, 40)]])
    info = extract_plate(str(src), 1, str(dst))
    assert info["origin_shift"] == [0, 0] and info["dropped_items"] == 1
    assert [(o, t[9]) for o, t in _items(dst)] == [("2", 50)]


def test_extract_maps_instances_of_one_object_across_plates(tmp_path):
    # object 2 has instance 0 on plate 1 and instance 1 on plate 2
    src, dst = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _project(src, [[("2", 50, 50)], [("2", 300, 30)]])
    extract_plate(str(src), 2, str(dst))
    assert [(o, t[9], t[10]) for o, t in _items(dst)] == [("2", 60, 30)]


def test_extract_drops_stale_sliced_output_of_other_plates(tmp_path):
    src, dst = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _project(src, [[("2", 50, 50)], [("4", 290, 40)]],
             extra_files=[("Metadata/plate_1.gcode", "old"), ("Metadata/plate_2.json", "{}")])
    extract_plate(str(src), 2, str(dst))
    with zipfile.ZipFile(dst) as z:
        names = z.namelist()
    assert "Metadata/plate_1.gcode" not in names
    assert "Metadata/plate_1.json" in names  # plate 2's json, renumbered


@pytest.mark.parametrize("plates,plate,msg", [
    ([[("2", 50, 50)]], 2, "not found"),
    ([[("2", 50, 50)], []], 2, "no objects"),
])
def test_extract_rejects_missing_or_empty_plate(tmp_path, plates, plate, msg):
    src = tmp_path / "in.3mf"
    _project(src, plates)
    with pytest.raises(PlateExtractError, match=msg):
        extract_plate(str(src), plate, str(tmp_path / "out.3mf"))


def test_extract_needs_bed_size_for_later_plates(tmp_path):
    src = tmp_path / "in.3mf"
    _project(src, [[("2", 50, 50)], [("4", 290, 40)]])
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n) for n in z.namelist()}
    files["Metadata/project_settings.config"] = b"{}"
    with zipfile.ZipFile(src, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    with pytest.raises(PlateExtractError, match="printable_area"):
        extract_plate(str(src), 2, str(tmp_path / "out.3mf"))


def test_extract_without_plate_data(tmp_path):
    src, dst = tmp_path / "in.3mf", tmp_path / "out.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    assert extract_plate(str(src), 1, str(dst))["has_plate_data"] is False
    assert zipfile.ZipFile(dst).read("3D/3dmodel.model") == b"<model/>"
    with pytest.raises(PlateExtractError, match="no plates"):
        extract_plate(str(src), 2, str(dst))


# ------------------------------------------------------------------ G-code guard

def _gcode_zip(path, names):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        for n in names:
            z.writestr(n, "G1 X1")


def test_error_when_requested_plate_has_no_gcode(tmp_path):
    p = str(tmp_path / "o.3mf")
    _gcode_zip(p, ["Metadata/plate_1.gcode"])
    assert _sliced_plate_gcode_error(p, 2) is not None
    assert _sliced_plate_gcode_error(p, 1) is None


def test_plate_zero_needs_any_gcode(tmp_path):
    p = str(tmp_path / "o.3mf")
    _gcode_zip(p, [])
    assert _sliced_plate_gcode_error(p, 0) is not None
    _gcode_zip(p, ["Metadata/plate_2.gcode"])
    assert _sliced_plate_gcode_error(p, 0) is None


def test_unreadable_3mf_is_an_error(tmp_path):
    p = tmp_path / "o.3mf"
    p.write_bytes(b"nope")
    assert _sliced_plate_gcode_error(str(p), 1) is not None


# ------------------------------------------------------------------ run_orcaslicer_task

def _run(tmp_path, src, plate_id, gcode_names, export="out.3mf"):
    out = tmp_path / "out"
    out.mkdir()
    captured = {}

    async def _fake(*args, **kwargs):
        captured["args"] = list(args)
        _gcode_zip(str(out / export), gcode_names)
        proc = AsyncMock()
        proc.returncode = 0
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.wait = AsyncMock()
        return proc

    job = {"status": "pending", "logger": JobLogger("biz349")}
    main.jobs["biz349"] = job
    try:
        with patch("asyncio.create_subprocess_exec", new=_fake):
            asyncio.run(run_orcaslicer_task("biz349", str(src), str(out), plate_id=plate_id,
                                            export_3mf=export, geometry_only_retry=False))
    finally:
        main.jobs.pop("biz349", None)
    return job, captured.get("args")


@pytest.mark.parametrize("plate_id", [1, 2])
def test_any_plate_is_sliced_as_plate_one_without_arrange(tmp_path, plate_id):
    src = tmp_path / "in.3mf"
    _project(src, [[("2", 50, 50)], [("4", 290, 40)]])
    job, args = _run(tmp_path, src, plate_id, ["Metadata/plate_1.gcode"])
    assert job["status"] == "completed"
    assert args[args.index("--slice") + 1] == "1"
    assert args[args.index("--arrange") + 1] == "0"
    sliced = args[-1]
    assert sliced.endswith(f"_plate{plate_id}.3mf")
    assert _plates(sliced) == ["1"] and len(_items(sliced)) == 1


def test_no_gcode_fails_job(tmp_path):
    src = tmp_path / "in.3mf"
    _project(src, [[("2", 50, 50)], [("4", 290, 40)]])
    job, _ = _run(tmp_path, src, 2, [])
    assert job["status"] == "failed"
    assert "no G-code" in job["error"]


def test_missing_plate_fails_before_orca_runs(tmp_path):
    src = tmp_path / "in.3mf"
    _project(src, [[("2", 50, 50)]])
    job, args = _run(tmp_path, src, 3, ["Metadata/plate_1.gcode"])
    assert job["status"] == "failed" and "plate 3" in job["error"]
    assert args is None


def test_input_without_plates_keeps_arrange(tmp_path):
    src = tmp_path / "in.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    job, args = _run(tmp_path, src, 1, ["Metadata/plate_1.gcode"])
    assert job["status"] == "completed"
    assert args[args.index("--arrange") + 1] == "1"
