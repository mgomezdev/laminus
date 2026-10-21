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
        # Catalog builds in the background; poll until ready (or give up quickly).
        for _ in range(20):
            health = tc.get("/api/health").json()
            if health.get("catalog_loaded"):
                break
            if not health.get("catalog_building"):
                break
            import time; time.sleep(0.1)
        if not health.get("catalog_loaded"):
            pytest.skip("Real Laminus catalog not loaded (OrcaSlicer not installed in this environment)")
        return TestClient(_real_app, raise_server_exceptions=True)
    return TestClient(_mock_mod.app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Field sets — the contract
# ---------------------------------------------------------------------------

HEALTH_FIELDS = {"status", "orca_version", "catalog_loaded", "catalog_building", "active_jobs"}
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


def test_profiles_broken_fields(client):
    """Regression: /api/profiles/broken must be registered before
    /api/profiles/{profile_uuid} - if that ordering regresses, this 404s (swallowed as a
    UUID lookup) instead of 200ing."""
    r = client.get("/api/profiles/broken")
    assert r.status_code == 200
    body = r.json()
    assert "count" in body and "broken" in body
    assert isinstance(body["broken"], list)


def test_profiles_rescan_starts(client):
    r = client.post("/api/profiles/rescan")
    assert r.status_code == 200
    assert "status" in r.json()


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


def test_known_profile_by_machine_name(client):
    """Requesting the default machine by name returns that same triple."""
    default = _known_profile(client)
    r = client.get("/api/test/known-profile", params={"machine_name": default["machine_name"]})
    assert r.status_code == 200
    assert r.json() == default


def test_known_profile_unknown_machine_name_404s(client):
    r = client.get("/api/test/known-profile", params={"machine_name": "No Such Printer Ever"})
    assert r.status_code == 404


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


@pytest.mark.parametrize(
    "export_name",
    ["/config/user/default/machine/evil.json", "../../../data/jobs.json", "nota3mf.txt"],
)
def test_slice_start_rejects_unsafe_export_3mf(client, export_name):
    """export_3mf reaches OrcaSlicer as --export-3mf and is joined onto the output
    dir, so a path component or a non-.3mf suffix must be refused. Mock and real
    must agree — otherwise a test passes against the mock while the real service
    rejects the identical request."""
    kp = _known_profile(client)
    r = client.post(
        "/api/slice/start",
        files={"file": ("m.stl", _stl(), "application/octet-stream")},
        data={
            "machine_uuid": kp["machine_uuid"],
            "process_uuid": kp["process_uuid"],
            "filament_uuids": json.dumps([kp["filament_uuid"]]),
            "plate": "1",
            "export_3mf": export_name,
        },
    )
    assert r.status_code == 422


def test_slice_prepared_fields(client):
    r = client.post(
        "/api/slice/prepared",
        files={"file": ("m.3mf", _minimal_3mf(), "application/octet-stream")},
        data={"plate": "1"},
    )
    assert r.status_code == 200
    assert "job_id" in r.json()


def test_slice_prepared_rejects_unsafe_export_3mf(client):
    r = client.post(
        "/api/slice/prepared",
        files={"file": ("m.3mf", _minimal_3mf(), "application/octet-stream")},
        data={"plate": "1", "export_3mf": "../../../data/jobs.json"},
    )
    assert r.status_code == 422


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


# ---------------------------------------------------------------------------
# Neutral (model / nozzle-variant) API
# ---------------------------------------------------------------------------

CANDIDATE_FIELDS = {"ref", "uuid", "name", "nozzle_diameter_mm", "flow", "confidence", "match_reason"}
VARIANT_FIELDS = {"ref", "uuid", "name", "nozzle_diameter_mm", "flow", "is_default_guess"}
PRESET_FIELDS = {"ref", "uuid", "name", "kind"}


def _model_query(client: TestClient) -> dict:
    m = next(m for m in client.get("/api/profiles").json()["machine"] if m.get("manufacturer") and m.get("model"))
    return {"manufacturer": m["manufacturer"], "model": m["model"]}


def _suggested_ref(client: TestClient) -> str:
    return client.post("/api/profiles/machines/suggest", json=_model_query(client)).json()["candidates"][0]["ref"]


def test_neutral_suggest_fields_and_version_header(client):
    r = client.post("/api/profiles/machines/suggest", json=_model_query(client))
    assert r.status_code == 200
    body = r.json()
    assert r.headers["X-Catalog-Version"] == body["catalog_version"]
    top = body["candidates"][0]
    assert not _missing(CANDIDATE_FIELDS, top)
    assert top["match_reason"] == "exact_model" and top["flow"] in ("standard", "high_flow")


def test_neutral_suggest_no_match_is_empty(client):
    r = client.post("/api/profiles/machines/suggest", json={"manufacturer": "Nobody", "model": "Zzz9000"})
    assert r.status_code == 200 and r.json()["candidates"] == []


def test_neutral_variants_one_default(client):
    v = client.post("/api/profiles/models/variants", json=_model_query(client)).json()["variants"]
    assert v and not _missing(VARIANT_FIELDS, v[0])
    assert sum(x["is_default_guess"] for x in v) == 1


def test_neutral_presets_fields_and_kind_filter(client):
    ref = _suggested_ref(client)
    presets = client.get(f"/api/profiles/machines/{ref}/presets").json()["presets"]
    assert {p["kind"] for p in presets} == {"process", "filament"}
    assert not _missing(PRESET_FIELDS, presets[0])
    only = client.get(f"/api/profiles/machines/{ref}/presets?kind=process").json()["presets"]
    assert only and {p["kind"] for p in only} == {"process"}


def test_neutral_presets_unknown_ref_404(client):
    r = client.get("/api/profiles/machines/machine:nope/presets")
    assert r.status_code == 404 and r.json()["error"] == "unknown_machine_ref"


def test_neutral_supports(client):
    ref = _suggested_ref(client)
    ok = client.get(f"/api/profiles/machines/{ref}/supports").json()
    assert ok["supported"] is True and ok["reason"] is None
    bad = client.get(f"/api/profiles/machines/{ref}/supports?bed_x=1&bed_y=1").json()
    assert bad["reason"] == "bed_mismatch"
    unk = client.get("/api/profiles/machines/machine:nope/supports")
    assert unk.status_code == 200 and unk.json()["reason"] == "unknown_machine_ref"


# ---------------------------------------------------------------------------
# /api/v1
# ---------------------------------------------------------------------------

JOB_VIEW_FIELDS = {"id", "status", "filename", "output_format", "error", "artifact", "links"}


def _v1_create_job(client: TestClient, headers: dict | None = None) -> "object":
    kp = _known_profile(client)
    return client.post(
        "/api/v1/jobs",
        files={"file": ("m.stl", _stl(), "application/octet-stream")},
        data={
            "machine_uuid": kp["machine_uuid"],
            "process_uuid": kp["process_uuid"],
            "filament_uuids": json.dumps([kp["filament_uuid"]]),
            "plate": "1",
        },
        headers=headers or {},
    )


def test_v1_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert not _missing(HEALTH_FIELDS, r.json())
    assert r.headers["API-Version"] == "v1" and r.headers["X-Request-ID"]


def test_v1_discovery(client):
    body = client.get("/api/v1").json()
    assert body["api_version"] == "v1" and "v1" in body["supported_versions"]
    assert "features" in body and "limits" in body


def test_v1_profiles_flat_typed_and_paginated(client):
    body = client.get("/api/v1/profiles").json()
    assert {"data", "page"} <= body.keys() and {"limit", "next_cursor"} <= body["page"].keys()
    assert {p["type"] for p in body["data"]} >= {"machine", "process", "filament"}
    assert all("uuid" in p for p in body["data"])
    only = client.get("/api/v1/profiles", params={"type": "machine"}).json()["data"]
    assert only and {p["type"] for p in only} == {"machine"}


def test_v1_profiles_cursor_walk_has_no_gaps_or_repeats(client):
    seen, cursor = [], None
    for _ in range(500):
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        body = client.get("/api/v1/profiles", params=params).json()
        seen += [p["uuid"] for p in body["data"]]
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) >= 3


def test_v1_profiles_bad_cursor_is_enveloped_422(client):
    r = client.get("/api/v1/profiles", params={"cursor": "not-a-cursor"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor"


def test_v1_profile_detail_and_404_envelope(client):
    kp = _known_profile(client)
    assert client.get(f"/api/v1/profiles/{kp['machine_uuid']}").json()["uuid"] == kp["machine_uuid"]
    r = client.get("/api/v1/profiles/no-such-uuid")
    assert r.status_code == 404 and r.json()["error"]["code"] == "profile_not_found"
    assert r.json()["request_id"] == r.headers["X-Request-ID"]


def test_v1_profiles_broken_not_swallowed_by_uuid_route(client):
    r = client.get("/api/v1/profiles/broken")
    assert r.status_code == 200 and "broken" in r.json()


def test_v1_profiles_rescan_and_merged_config(client):
    assert client.post("/api/v1/profiles/rescan").status_code == 200
    kp = _known_profile(client)
    r = client.post("/api/v1/profiles/merged-config", json={
        "machine_uuid": kp["machine_uuid"], "process_uuid": kp["process_uuid"],
        "filament_uuids": [kp["filament_uuid"]]})
    assert r.status_code == 200 and isinstance(r.json(), dict) and r.json()


def test_v1_neutral_profile_routes(client):
    q = _model_query(client)
    cands = client.post("/api/v1/profiles/machines/suggest", json=q).json()["candidates"]
    assert cands and not _missing(CANDIDATE_FIELDS, cands[0])
    variants = client.post("/api/v1/profiles/models/variants", json=q).json()["variants"]
    assert variants and not _missing(VARIANT_FIELDS, variants[0])
    ref = cands[0]["ref"]
    presets = client.get(f"/api/v1/profiles/machines/{ref}/presets").json()["presets"]
    assert {p["kind"] for p in presets} == {"process", "filament"}
    assert client.get(f"/api/v1/profiles/machines/{ref}/supports").json()["supported"] is True


def test_v1_job_lifecycle(client):
    r = _v1_create_job(client)
    assert r.status_code == 202, r.text[:200]
    job = r.json()
    assert not _missing(JOB_VIEW_FIELDS, job)
    assert r.headers["Location"] == job["links"]["self"]
    for _ in range(30):
        view = client.get(f"/api/v1/jobs/{job['id']}").json()
        if view["status"] in ("completed", "failed"):
            break
    assert view["status"] == "completed" and view["artifact"]["name"]
    a = client.get(f"/api/v1/jobs/{job['id']}/artifact")
    assert a.status_code == 200 and len(a.content) > 0
    again = client.get(f"/api/v1/jobs/{job['id']}/artifact")
    assert again.status_code == 404 and again.json()["error"]["code"] == "artifact_gone"
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["artifact"]["available"] is False


def test_v1_job_list_is_paginated(client):
    _v1_create_job(client)
    body = client.get("/api/v1/jobs", params={"limit": 1}).json()
    assert {"data", "page"} <= body.keys() and len(body["data"]) == 1


def test_v1_job_404_is_enveloped(client):
    r = client.get("/api/v1/jobs/nonexistent-id-xyz")
    assert r.status_code == 404 and r.json()["error"]["code"] == "job_not_found"


def test_v1_unknown_path_is_enveloped(client):
    r = client.get("/api/v1/definitely-not-a-route")
    assert r.status_code == 404 and "error" in r.json()


def test_v1_validation_error_is_enveloped(client):
    r = client.post("/api/v1/jobs", files={"file": ("m.stl", _stl())}, data={"plate": "1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_v1_job_idempotency_replay(client):
    key = {"Idempotency-Key": "contract-idem-1"}
    first = _v1_create_job(client, key)
    second = _v1_create_job(client, key)
    assert first.status_code == second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert second.headers["Idempotent-Replay"] == "true"


def test_v1_slice_prepared(client):
    r = client.post("/api/v1/slice/prepared",
                    files={"file": ("m.3mf", _minimal_3mf(), "application/octet-stream")}, data={"plate": "1"})
    assert r.status_code == 200 and "job_id" in r.json()


def test_v1_pack_and_arrange_return_zip(client):
    r = client.post("/api/v1/pack", files=[("files", ("m.stl", _stl(), "application/octet-stream"))],
                    data={"bed_x": "200", "bed_y": "200", "bed_z": "200"})
    assert r.status_code == 200 and zipfile.is_zipfile(io.BytesIO(r.content))
    r = client.post("/api/v1/arrange", files={"file": ("m.3mf", _minimal_3mf(), "application/octet-stream")},
                    data={"arrange": "true", "orient": "true"})
    assert r.status_code == 200 and zipfile.is_zipfile(io.BytesIO(r.content))
