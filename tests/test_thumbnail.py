"""Tests for POST /api/slice/thumbnail."""
import io
import zipfile
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app


def _make_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


async def _mock_slicer_ok(*args, **kwargs):
    """Write a 3MF with a plate_1 PNG, return a successful mock process."""
    args_list = list(args)
    out_path = args_list[args_list.index("--export-3mf") + 1]
    plate = int(args_list[args_list.index("--slice") + 1])
    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr(f"Metadata/plate_{plate}.png", _PNG_HEADER + b"\x00" * 8)
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate.return_value = (b"Done\n", None)
    return proc


async def _mock_slicer_plate1_only(*args, **kwargs):
    """OrcaSlicer always outputs plate_1.png regardless of requested plate."""
    args_list = list(args)
    out_path = args_list[args_list.index("--export-3mf") + 1]
    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr("Metadata/plate_1.png", _PNG_HEADER + b"\x00" * 8)
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate.return_value = (b"Done\n", None)
    return proc


async def _mock_slicer_fail(*args, **kwargs):
    proc = AsyncMock()
    proc.returncode = 1
    proc.communicate.return_value = (b"Error\n", None)
    return proc


def test_thumbnail_returns_png():
    client = TestClient(app)
    with patch("asyncio.create_subprocess_exec", new=_mock_slicer_ok):
        resp = client.post(
            "/api/slice/thumbnail",
            files={"file": ("model.3mf", _make_3mf(), "application/octet-stream")},
            data={"plate": "1"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content[:8] == _PNG_HEADER


def test_thumbnail_fallback_to_plate_1():
    """When OrcaSlicer outputs plate_1.png regardless of the requested plate, we still return it."""
    client = TestClient(app)
    with patch("asyncio.create_subprocess_exec", new=_mock_slicer_plate1_only):
        resp = client.post(
            "/api/slice/thumbnail",
            files={"file": ("model.3mf", _make_3mf(), "application/octet-stream")},
            data={"plate": "2"},
        )
    assert resp.status_code == 200
    assert resp.content[:8] == _PNG_HEADER


def test_thumbnail_422_on_nonzero_exit():
    client = TestClient(app)
    with patch("asyncio.create_subprocess_exec", new=_mock_slicer_fail):
        resp = client.post(
            "/api/slice/thumbnail",
            files={"file": ("model.3mf", _make_3mf(), "application/octet-stream")},
            data={"plate": "1"},
        )
    assert resp.status_code == 422
    assert "error" in resp.json()["detail"]


def test_thumbnail_rejects_non_3mf():
    client = TestClient(app)
    resp = client.post(
        "/api/slice/thumbnail",
        files={"file": ("model.stl", b"solid test\nendsolid", "application/octet-stream")},
        data={"plate": "1"},
    )
    assert resp.status_code == 422


def test_thumbnail_rejects_plate_zero():
    client = TestClient(app)
    resp = client.post(
        "/api/slice/thumbnail",
        files={"file": ("model.3mf", _make_3mf(), "application/octet-stream")},
        data={"plate": "0"},
    )
    assert resp.status_code == 422
