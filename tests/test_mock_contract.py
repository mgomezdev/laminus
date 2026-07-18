"""Dual-TestClient contract test: real app and mock must satisfy identical assertions.

Both real_client and mock_client are tested against the same test functions.
A PR that changes the API must also update the mock — or this test fails.

Run:
    pytest tests/test_mock_contract.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import struct
import sys
import zipfile
from pathlib import Path
from typing import Callable

import pytest

fastapi = pytest.importorskip("fastapi", reason="fastapi required")
from fastapi.testclient import TestClient  # noqa: E402

_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Load real app and mock app
# ---------------------------------------------------------------------------

def _load_mock() -> object:
    """Load mock/server.py as a module."""
    p = _ROOT / "mock" / "server.py"
    spec = importlib.util.spec_from_file_location("laminus_mock_server", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mock_mod = _load_mock()

# Try to import the real app; skip if unavailable (e.g. OrcaSlicer not installed in CI)
_real_app = None
try:
    sys.path.insert(0, str(_ROOT))
    from app.main import app as _real_app_obj  # noqa: E402
    _real_app = _real_app_obj
except Exception as e:
    pass  # real app unavailable; only mock tests run


# ---------------------------------------------------------------------------
# Fixtures — parametrize the test suite over both clients
# ---------------------------------------------------------------------------

@pytest.fixture(params=["mock", "real"])
def client(request):
    if request.param == "real":
        if _real_app is None:
            pytest.skip("Real Laminus app not importable")
        tc = TestClient(_real_app, raise_server_exceptions=False)
        health = tc.get("/api/health").json()
        if not health.get("catalog_loaded"):
            pytest.skip("Real Laminus catalog not loaded (OrcaSlicer not installed in this environment)")
        return TestClient(_real_app, raise_server_exceptions=True)
    return TestClient(_mock_mod.app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Field sets — the contract
# ---------------------------------------------------------------------------

HEALTH_FIELDS = {"status", "catalog_loaded", "catalog_building", "active_jobs"}
MACHINE_FIELDS = {"uuid", "name", "manufacturer", "model", "nozzle", "bed_size_x", "bed_size_y", "extruder_count"}
PROCESS_FIELDS = {"uuid", "name", "layer_height", "compatible_printers"}
FILAMENT_FIELDS = {"uuid", "name", "filament_type", "compatible_printers"}
SLICE_START_FIELDS = {"job_id", "status"}
SLICE_STATUS_FIELDS = {"job_id", "status", "output_format", "sliced_file", "error"}
KNOWN_PROFILE_FIELDS = {"machine_uuid", "machine_name", "process_uuid", "process_name", "filament_uuid", "filament_name"}


def _missing(required: set, actual: dict) -> set:
    return required - actual.keys()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stl() -> bytes:
    buf = io.BytesIO()
    buf.write(b"contract-test".ljust(80))
    buf.write(struct.pack("<I", 1))
    for f in (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0):
        buf.write(struct.pack("<f", f))
    buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def _minimal_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(
            "Metadata/project_settings.config",
            json.dumps({"printable_area": ["0x0", "200x0", "200x200", "0x200"], "printable_height": 200}),
        )
    return buf.getvalue()


def _known_profile(client: TestClient) -> dict:
    r = client.get("/api/test/known-profile")
    assert r.status_code == 200
    return r.json()


def _start_job(client: TestClient, kp: dict) -> str:
    r = client.post(
        "/api/slice/start",
        files={"file": ("m.stl", _stl(), "application/octet-stream")},
        data={
            "machine_uuid": kp["machine_uuid"],
            "process_uuid": kp["process_uuid"],
            "filament_uuids": json.dumps([kp["filament_uuid"]]),
            "plate": "1",
        },
    )
    assert r.status_code == 200, f"slice/start failed: {r.text[:200]}"
    return r.json()["job_id"]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_200(client):
    assert client.get("/api/health").status_code == 200


def test_health_fields(client):
    data = client.get("/api/health").json()
    assert not _missing(HEALTH_FIELDS, data), _missing(HEALTH_FIELDS, data)


def test_health_catalog_ready(client):
    data = client.get("/api/health").json()
    assert data["catalog_loaded"] is True
    assert data["catalog_building"] is False
    assert isinstance(data["active_jobs"], int)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def test_profiles_top_level_keys(client):
    data = client.get("/api/profiles").json()
    assert {"machine", "process", "filament"} <= data.keys()


def test_profiles_non_empty(client):
    data = client.get("/api/profiles").json()
    assert len(data["machine"]) >= 1
    assert len(data["process"]) >= 1
    assert len(data["filament"]) >= 1


def test_profiles_machine_fields(client):
    m = client.get("/api/profiles").json()["machine"][0]
    assert not _missing(MACHINE_FIELDS, m), _missing(MACHINE_FIELDS, m)


def test_profiles_process_fields(client):
    p = client.get("/api/profiles").json()["process"][0]
    assert not _missing(PROCESS_FIELDS, p), _missing(PROCESS_FIELDS, p)


def test_profiles_filament_fields(client):
    f = client.get("/api/profiles").json()["filament"][0]
    assert not _missing(FILAMENT_FIELDS, f), _missing(FILAMENT_FIELDS, f)


def test_profile_detail_by_uuid(client):
    kp = _known_profile(client)
    for uid in (kp["machine_uuid"], kp["process_uuid"], kp["filament_uuid"]):
        r = client.get(f"/api/profiles/{uid}")
        assert r.status_code == 200
        assert r.json()["uuid"] == uid


def test_profile_detail_404(client):
    assert client.get("/api/profiles/no-such-uuid").status_code == 404


def test_merged_config_returns_dict(client):
    kp = _known_profile(client)
    r = client.post(
        "/api/profiles/merged-config",
        json={
            "machine_uuid": kp["machine_uuid"],
            "process_uuid": kp["process_uuid"],
            "filament_uuids": [kp["filament_uuid"]],
        },
    )
    assert r.status_code == 200
    assert isinstance(r.json(), dict)
    assert len(r.json()) > 0


# ---------------------------------------------------------------------------
# Known-profile helper endpoint
# ---------------------------------------------------------------------------

def test_known_profile_fields(client):
    r = client.get("/api/test/known-profile")
    assert r.status_code == 200
    assert not _missing(KNOWN_PROFILE_FIELDS, r.json()), _missing(KNOWN_PROFILE_FIELDS, r.json())


def test_known_profile_uuids_in_catalog(client):
    kp = _known_profile(client)
    for field, uid in [("machine_uuid", kp["machine_uuid"]), ("process_uuid", kp["process_uuid"]), ("filament_uuid", kp["filament_uuid"])]:
        r = client.get(f"/api/profiles/{uid}")
        assert r.status_code == 200, f"{field} {uid!r} not in catalog"


# ---------------------------------------------------------------------------
# Slice lifecycle
# ---------------------------------------------------------------------------

def test_slice_start_fields(client):
    kp = _known_profile(client)
    r = client.post(
        "/api/slice/start",
        files={"file": ("m.stl", _stl(), "application/octet-stream")},
        data={
            "machine_uuid": kp["machine_uuid"],
            "process_uuid": kp["process_uuid"],
            "filament_uuids": json.dumps([kp["filament_uuid"]]),
            "plate": "1",
        },
    )
    assert r.status_code == 200
    assert not _missing(SLICE_START_FIELDS, r.json())


def test_slice_prepared_fields(client):
    r = client.post(
        "/api/slice/prepared",
        files={"file": ("m.3mf", _minimal_3mf(), "application/octet-stream")},
        data={"plate": "1"},
    )
    assert r.status_code == 200
    assert "job_id" in r.json()


def test_slice_status_fields(client):
    kp = _known_profile(client)
    job_id = _start_job(client, kp)
    # Poll until terminal (mock completes instantly; real may need retries)
    for _ in range(30):
        r = client.get(f"/api/slice/status/{job_id}")
        assert r.status_code == 200
        data = r.json()
        assert not _missing(SLICE_STATUS_FIELDS, data)
        if data["status"] in ("completed", "failed"):
            break
    assert data["status"] in ("completed", "failed"), f"Job still {data['status']} after 30 polls"
    assert data["error"] is None or isinstance(data["error"], str)


def test_slice_download_returns_bytes(client):
    kp = _known_profile(client)
    job_id = _start_job(client, kp)
    # Wait for completion
    for _ in range(30):
        r = client.get(f"/api/slice/status/{job_id}")
        if r.json().get("status") == "completed":
            break
    r = client.get(f"/api/slice/download/{job_id}")
    assert r.status_code == 200
    assert len(r.content) > 0


def test_slice_status_404_unknown(client):
    assert client.get("/api/slice/status/nonexistent-id-xyz").status_code == 404


# ---------------------------------------------------------------------------
# Pack / Arrange
# ---------------------------------------------------------------------------

def test_pack_returns_zip(client):
    r = client.post(
        "/api/pack",
        files=[("files", ("m.stl", _stl(), "application/octet-stream"))],
        data={"bed_x": "200", "bed_y": "200", "bed_z": "200"},
    )
    assert r.status_code == 200
    assert zipfile.is_zipfile(io.BytesIO(r.content))


def test_arrange_returns_zip(client):
    r = client.post(
        "/api/arrange",
        files={"file": ("m.3mf", _minimal_3mf(), "application/octet-stream")},
        data={"arrange": "true", "orient": "true"},
    )
    assert r.status_code == 200
    assert zipfile.is_zipfile(io.BytesIO(r.content))
