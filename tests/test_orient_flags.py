"""--orient tilts parts about X/Y (changes layer-line direction): pack never passes it
as 1, and /api/arrange only passes it when orient=true is requested."""
import io
import struct
import zipfile
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _stl() -> bytes:
    buf = io.BytesIO()
    buf.write(b"orient-test".ljust(80))
    buf.write(struct.pack("<I", 1))
    for f in (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0):
        buf.write(struct.pack("<f", f))
    buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def _3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _run(path, **post):
    captured = {}

    async def _fake(*args, **kwargs):
        captured["args"] = list(args)
        out = args[args.index("--export-3mf") + 1]
        with open(out, "wb") as fh:
            fh.write(_3mf())
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"Done\n", None))
        return proc

    with patch("asyncio.create_subprocess_exec", new=_fake):
        resp = TestClient(app).post(path, **post)
    assert resp.status_code == 200, resp.text
    return captured["args"]


def _orient_value(args):
    return args[args.index("--orient") + 1] if "--orient" in args else None


@pytest.mark.parametrize("path", ["/api/pack", "/api/v1/pack"])
def test_pack_never_orients(path):
    args = _run(path, files=[("files", ("a.stl", _stl(), "application/octet-stream"))],
                data={"bed_x": "200", "bed_y": "200", "bed_z": "200"})
    assert _orient_value(args) in (None, "0")


@pytest.mark.parametrize("data,expected", [({}, None), ({"orient": "false"}, None), ({"orient": "true"}, "1")])
def test_arrange_orient_is_opt_in(data, expected):
    args = _run("/api/arrange", files={"file": ("m.3mf", _3mf(), "application/octet-stream")}, data=data)
    assert _orient_value(args) == expected
