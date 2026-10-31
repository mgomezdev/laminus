"""BIZ-349: slicing plate >=2 of a multi-plate 3MF must not report `completed` with no G-code,
and multi-plate projects must not be sent through `--arrange 1` (it collapses the plates)."""
import asyncio
import io
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from app import main
from app.main import _sliced_plate_gcode_error, _plate_count_or_one, run_orcaslicer_task, JobLogger


def _zip(path, names, plates=2):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        ms = '<config>' + '<plate/>' * plates + '</config>'
        z.writestr("Metadata/model_settings.config", ms)
        for n in names:
            z.writestr(n, "G1 X1")


def test_error_when_requested_plate_has_no_gcode(tmp_path):
    p = str(tmp_path / "o.3mf")
    _zip(p, ["Metadata/plate_1.gcode"])
    assert _sliced_plate_gcode_error(p, 2) is not None
    assert _sliced_plate_gcode_error(p, 1) is None


def test_plate_zero_needs_any_gcode(tmp_path):
    p = str(tmp_path / "o.3mf")
    _zip(p, [])
    assert _sliced_plate_gcode_error(p, 0) is not None
    _zip(p, ["Metadata/plate_2.gcode"])
    assert _sliced_plate_gcode_error(p, 0) is None


def test_unreadable_3mf_is_an_error(tmp_path):
    p = tmp_path / "o.3mf"
    p.write_bytes(b"nope")
    assert _sliced_plate_gcode_error(str(p), 1) is not None


def test_plate_count(tmp_path):
    p = str(tmp_path / "i.3mf")
    _zip(p, [], plates=3)
    assert _plate_count_or_one(p) == 3
    assert _plate_count_or_one(str(tmp_path / "missing.3mf")) == 1


def _run(tmp_path, plates, plate_id, gcode_names, export="out.3mf"):
    src = str(tmp_path / "in.3mf")
    _zip(src, [], plates=plates)
    out = tmp_path / "out"
    out.mkdir()
    captured = {}

    async def _fake(*args, **kwargs):
        captured["args"] = list(args)
        _zip(str(out / export), gcode_names, plates=plates)
        proc = AsyncMock()
        proc.returncode = 0
        proc.stdout.readline = AsyncMock(return_value=b"")
        proc.wait = AsyncMock()
        return proc

    job = {"status": "pending", "logger": JobLogger("biz349")}
    main.jobs["biz349"] = job
    try:
        with patch("asyncio.create_subprocess_exec", new=_fake):
            asyncio.run(run_orcaslicer_task("biz349", src, str(out), plate_id=plate_id,
                                            export_3mf=export, geometry_only_retry=False))
    finally:
        main.jobs.pop("biz349", None)
    return job, captured["args"]


def test_plate2_without_gcode_fails_job(tmp_path):
    job, _ = _run(tmp_path, 2, 2, [])
    assert job["status"] == "failed"
    assert "no G-code" in job["error"]


def test_plate2_with_gcode_completes(tmp_path):
    job, _ = _run(tmp_path, 2, 2, ["Metadata/plate_2.gcode"])
    assert job["status"] == "completed"


@pytest.mark.parametrize("plates,expected", [(1, "1"), (2, "0")])
def test_arrange_only_for_single_plate(tmp_path, plates, expected):
    _, args = _run(tmp_path, plates, 1, ["Metadata/plate_1.gcode"])
    assert args[args.index("--arrange") + 1] == expected
