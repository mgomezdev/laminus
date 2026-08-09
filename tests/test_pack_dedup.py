"""Regression: /api/pack must not silently overwrite two uploaded STLs sharing a
filename - both must be written and both must reach the packer."""
import io
import os
import struct
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app


def _stl() -> bytes:
    buf = io.BytesIO()
    buf.write(b"dedup-test".ljust(80))
    buf.write(struct.pack("<I", 1))
    for f in (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0):
        buf.write(struct.pack("<f", f))
    buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def test_pack_dedupes_duplicate_stl_filenames():
    captured = {}

    def _fake_inject(template_path, stl_paths, out_path):
        # Capture + verify existence here, while the job directory is still alive -
        # background_tasks cleanup deletes it once the response has been sent.
        captured["stl_paths"] = list(stl_paths)
        captured["all_exist"] = all(os.path.exists(p) for p in stl_paths)
        with open(out_path, "wb") as fh:
            fh.write(b"not-a-real-3mf")

    async def _mock_slicer_ok(*args, **kwargs):
        args_list = list(args)
        out_path = args_list[args_list.index("--export-3mf") + 1]
        with open(out_path, "wb") as fh:
            fh.write(b"not-a-real-3mf")
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"Done\n", None))
        return proc

    client = TestClient(app)
    with patch("app.main._inject_stls_into_3mf", new=_fake_inject), \
         patch("asyncio.create_subprocess_exec", new=_mock_slicer_ok):
        resp = client.post(
            "/api/pack",
            files=[
                ("files", ("bracket.stl", _stl(), "application/octet-stream")),
                ("files", ("bracket.stl", _stl(), "application/octet-stream")),
            ],
            data={"bed_x": "200", "bed_y": "200", "bed_z": "200"},
        )

    assert resp.status_code == 200
    stl_paths = captured["stl_paths"]
    assert len(stl_paths) == 2
    assert len(set(stl_paths)) == 2, f"duplicate STL filenames collided on disk: {stl_paths}"
    assert captured["all_exist"], "not all deduplicated STLs existed on disk at injection time"
