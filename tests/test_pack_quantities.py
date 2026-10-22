"""/api/pack `quantities`: validation + pass-through to the injector."""
import io
import struct
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app, PACK_MAX_INSTANCES


def _stl() -> bytes:
    buf = io.BytesIO()
    buf.write(b"qty-test".ljust(80))
    buf.write(struct.pack("<I", 1))
    for f in (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0):
        buf.write(struct.pack("<f", f))
    buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def _post(client, n_files, quantities=None):
    data = {"bed_x": "200", "bed_y": "200", "bed_z": "200"}
    if quantities is not None:
        data["quantities"] = quantities
    return client.post(
        "/api/pack",
        files=[("files", (f"p{i}.stl", _stl(), "application/octet-stream")) for i in range(n_files)],
        data=data,
    )


@pytest.mark.parametrize("quantities", [
    "[1]",                      # length mismatch (2 files)
    "[1, 0]",                   # qty < 1
    "[1, -2]",
    "not json",
    '{"a": 1}',
    "[1, 2.5]",
    "[true, 1]",
    f"[{PACK_MAX_INSTANCES}, 1]",  # over total cap
])
def test_pack_rejects_bad_quantities(quantities):
    resp = _post(TestClient(app), 2, quantities)
    assert resp.status_code == 422, resp.text


def _run(quantities):
    captured = {}

    def _fake_inject(template_path, stl_paths, out_path, quantities=None):
        captured["quantities"] = quantities
        with open(out_path, "wb") as fh:
            fh.write(b"x")

    async def _mock_slicer_ok(*args, **kwargs):
        out_path = list(args)[list(args).index("--export-3mf") + 1]
        with open(out_path, "wb") as fh:
            fh.write(b"x")
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"Done\n", None))
        return proc

    with patch("app.main._inject_stls_into_3mf", new=_fake_inject), \
         patch("asyncio.create_subprocess_exec", new=_mock_slicer_ok):
        resp = _post(TestClient(app), 2, quantities)
    return resp, captured


def test_pack_passes_quantities_to_injector():
    resp, captured = _run("[3, 1]")
    assert resp.status_code == 200
    assert captured["quantities"] == [3, 1]


def test_pack_without_quantities_is_legacy():
    resp, captured = _run(None)
    assert resp.status_code == 200
    assert captured["quantities"] is None
