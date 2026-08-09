"""Regression: /api/pack and /api/arrange must kill the OrcaSlicer subprocess on timeout
instead of leaving it (and its Xvfb) running against a deleted job directory."""
import asyncio
import io
import struct
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


def _stl() -> bytes:
    buf = io.BytesIO()
    buf.write(b"timeout-test".ljust(80))
    buf.write(struct.pack("<I", 1))
    for f in (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0):
        buf.write(struct.pack("<f", f))
    buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def _minimal_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _make_timeout_subprocess(processes: list):
    """create_subprocess_exec replacement: communicate() raises TimeoutError, same as a
    real asyncio.wait_for(process.communicate(), timeout=...) would on expiry."""
    async def _create(*args, **kwargs):
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.returncode = None
        processes.append(proc)
        return proc
    return _create


def test_pack_kills_process_on_timeout():
    processes: list = []
    client = TestClient(app)
    with patch("asyncio.create_subprocess_exec", new=_make_timeout_subprocess(processes)):
        resp = client.post(
            "/api/pack",
            files=[("files", ("m.stl", _stl(), "application/octet-stream"))],
            data={"bed_x": "200", "bed_y": "200", "bed_z": "200"},
        )
    assert resp.status_code == 408
    assert len(processes) == 1
    processes[0].kill.assert_called_once()
    processes[0].wait.assert_awaited_once()


def test_arrange_kills_process_on_timeout():
    processes: list = []
    client = TestClient(app)
    with patch("asyncio.create_subprocess_exec", new=_make_timeout_subprocess(processes)):
        resp = client.post(
            "/api/arrange",
            files={"file": ("m.3mf", _minimal_3mf(), "application/octet-stream")},
            data={"arrange": "true", "orient": "true"},
        )
    assert resp.status_code == 408
    assert len(processes) == 1
    processes[0].kill.assert_called_once()
    processes[0].wait.assert_awaited_once()
