"""Verify the deployed Laminus API with the real OrcaSlicer 2.5.1 release."""

import json
import os
import time

import httpx
import pytest

from tests.test_pack_quantities_live import analyse, cube_stl


BASE = os.environ.get("LAMINUS_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="requires a live Laminus and OrcaSlicer")
PRESETS = {
    "machine": "Elegoo Centauri Carbon 0.4 nozzle",
    "process": "0.20mm Standard @Elegoo CC 0.4 nozzle",
    "filament": "Elegoo PLA Basic @ECC",
}


@pytest.fixture(scope="module")
def live_api():
    with httpx.Client(base_url=BASE, timeout=60) as client:
        deadline = time.monotonic() + 240
        health = None
        while time.monotonic() < deadline:
            try:
                response = client.get("/api/v1/health", timeout=5)
                health = response.json() if response.is_success else response.status_code
            except httpx.HTTPError as exc:
                health = str(exc)
            if isinstance(health, dict) and health["catalog_loaded"]:
                yield client
                return
            time.sleep(2)
        pytest.fail(f"Laminus catalog did not load: {health}")


def test_v1_slices_with_orca_25(live_api):
    prefix = "/api/v1"
    discovery = live_api.get(prefix)
    assert discovery.headers["API-Version"] == "v1"
    assert "Deprecation" not in discovery.headers
    assert discovery.json()["supported_versions"] == ["v1"]
    assert discovery.json()["engine"]["version"] == "2.5.1"
    health = live_api.get(f"{prefix}/health").json()
    assert health["orca_version"] == "2.5.1"
    assert health["orcaslicer_installed"]
    assert all(health["catalog_profile_count"][kind] > 0 for kind in ("machine", "process", "filament"))

    cube = ("cube.stl", cube_stl(20), "application/octet-stream")
    packed = live_api.post(f"{prefix}/pack", files={"files": cube},
                         data={"bed_x": 100, "bed_y": 100, "bed_z": 100})
    assert packed.status_code == 200, packed.text
    assert analyse(packed.content) == ({20: 1}, 1)

    uuids = {}
    for kind, name in PRESETS.items():
        profiles = live_api.get(f"{prefix}/profiles", params={"type": kind, "q": name})
        assert profiles.status_code == 200, profiles.text
        matches = [item["uuid"] for item in profiles.json()["data"] if item["name"] == name]
        assert len(matches) == 1, (name, matches)
        uuids[kind] = matches[0]
    job = live_api.post(f"{prefix}/jobs", files={"file": cube}, data={
        "machine_uuid": uuids["machine"], "process_uuid": uuids["process"],
        "filament_uuids": json.dumps([uuids["filament"]]), "plate": 1,
    })
    assert job.status_code == 202, job.text
    location = job.headers["Location"]
    assert location == f"{prefix}/jobs/{job.json()['id']}"

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = live_api.get(location).json()
        if status["status"] in {"completed", "failed"}:
            break
        time.sleep(1)
    assert status["status"] == "completed", status
    assert status["links"]["artifact"] == f"{location}/artifact"
    with live_api.stream("GET", status["links"]["events"], timeout=10) as events:
        assert events.headers["content-type"].startswith("text/event-stream")
        assert any(line == "event: completed" for line in events.iter_lines())
    artifact = live_api.get(status["links"]["artifact"])
    assert artifact.status_code == 200, artifact.text
    assert b"G1 " in artifact.content


def test_openapi_dashboard_and_routes_expose_only_v1(live_api):
    spec = live_api.get("/openapi.json").json()
    assert live_api.get("/openapi-v1.json").json() == spec
    paths = spec["paths"]
    assert all(path == "/api/v1" or path.startswith("/api/v1/") for path in paths)
    assert not paths["/api/v1/jobs"]["post"].get("deprecated")
    assert "/api/v1/jobs" in live_api.get("/").text
    for path in ("/api/v2", "/api/v2/health", "/api/v2/jobs", "/openapi-v2.json"):
        assert live_api.get(path).status_code == 404
