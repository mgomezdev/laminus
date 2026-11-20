"""Run the same requests through both versions of the deployed Laminus API."""

import json
import os
import time

import httpx
import pytest

from tests.test_pack_quantities_live import analyse, cube_stl


BASE = os.environ.get("LAMINUS_BRIDGE_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="requires both real Orca engines")
MACHINE = "Elegoo Centauri Carbon 0.4 nozzle"


@pytest.fixture(scope="module")
def bridge():
    with httpx.Client(base_url=BASE, timeout=60) as client:
        deadline = time.monotonic() + 240
        health = {}
        while time.monotonic() < deadline:
            for version in ("v1", "v2"):
                try:
                    response = client.get(f"/api/{version}/health", timeout=5)
                    health[version] = response.json() if response.is_success else response.status_code
                except httpx.HTTPError as exc:
                    health[version] = str(exc)
            if all(isinstance(report, dict) and report["catalog_loaded"] for report in health.values()):
                yield client
                return
            time.sleep(2)
        pytest.fail(f"Both Laminus catalogs must load: {health}")


@pytest.mark.parametrize(("version", "other", "release"), [
    ("v1", "v2", "2.4.2"),
    ("v2", "v1", "2.5.1"),
])
def test_real_engines_keep_jobs_isolated(bridge, version, other, release):
    prefix = f"/api/{version}"
    discovery = bridge.get(prefix)
    assert discovery.headers["API-Version"] == version
    assert ("Deprecation" in discovery.headers) == (version == "v1")
    assert discovery.json()["deprecated"] == (version == "v1")
    assert discovery.json()["supported_versions"] == ["v1", "v2"]
    assert discovery.json()["engine"]["version"] == release
    health = bridge.get(f"{prefix}/health").json()
    assert health["orcaslicer_installed"]
    assert all(health["catalog_profile_count"][kind] > 0 for kind in ("machine", "process", "filament"))

    cube = ("cube.stl", cube_stl(20), "application/octet-stream")
    packed = bridge.post(f"{prefix}/pack", files={"files": cube},
                         data={"bed_x": 100, "bed_y": 100, "bed_z": 100})
    assert packed.status_code == 200, packed.text
    assert analyse(packed.content) == ({20: 1}, 1)

    profiles = bridge.get(f"{prefix}/test/known-profile", params={"machine_name": MACHINE})
    assert profiles.status_code == 200, profiles.text
    profile = profiles.json()
    job = bridge.post(f"{prefix}/jobs", files={"file": cube}, data={
        "machine_uuid": profile["machine_uuid"], "process_uuid": profile["process_uuid"],
        "filament_uuids": json.dumps([profile["filament_uuid"]]), "plate": 1,
    })
    assert job.status_code == 202, job.text
    location = job.headers["Location"]
    assert location == f"{prefix}/jobs/{job.json()['id']}"
    assert bridge.get(location.replace(prefix, f"/api/{other}", 1)).status_code == 404

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = bridge.get(location).json()
        if status["status"] in {"completed", "failed"}:
            break
        time.sleep(1)
    assert status["status"] == "completed", status
    assert status["links"]["artifact"] == f"{location}/artifact"
    artifact = bridge.get(status["links"]["artifact"])
    assert artifact.status_code == 200, artifact.text
    assert b"G1 " in artifact.content


def test_openapi_and_dashboard_use_versioned_routes(bridge):
    combined = bridge.get("/openapi.json").json()["paths"]
    for version in ("v1", "v2"):
        prefix = f"/api/{version}"
        paths = bridge.get(f"/openapi-{version}.json").json()["paths"]
        assert paths
        assert all(path == prefix or path.startswith(f"{prefix}/") for path in paths)
        assert paths[f"{prefix}/jobs"]["post"].get("deprecated", False) == (version == "v1")
        assert combined[f"{prefix}/jobs"] == paths[f"{prefix}/jobs"]
    assert "/api/v2/jobs" in bridge.get("/").text
