"""Regression: MAX_CONCURRENT_JOBS must bound "pending" jobs too, not just "slicing" -
a job stays "pending" until its background task runs, which Starlette only does after
the response is sent, so a burst of concurrent requests would otherwise all be admitted.
"""
from fastapi.testclient import TestClient

import app.main as main_mod
from app.profile_catalog import ProfileCatalog


def _catalog_and_uuids(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    data = cat.as_dict()
    machine = data["machine"][0]
    process = next(p for p in data["process"] if p["name"] == "0.20mm Standard @BBL X1E")
    filament = data["filament"][0]
    return cat, machine["uuid"], process["uuid"], filament["uuid"]


def test_slice_start_rejects_when_pending_jobs_fill_the_limit(profile_tree):
    cat, machine_uuid, process_uuid, filament_uuid = _catalog_and_uuids(profile_tree)
    original_catalog = main_mod.catalog
    main_mod.catalog = cat
    client = TestClient(main_mod.app)

    for i in range(main_mod.MAX_CONCURRENT_JOBS):
        job_id = f"pending-job-{i}"
        main_mod.jobs[job_id] = {
            "id": job_id, "status": "pending", "error": None,
            "sliced_file": None, "logger": None, "created_at": 0,
            "_wall_created_at": 0,
        }
    try:
        resp = client.post(
            "/api/slice/start",
            files={"file": ("m.stl", b"solid t\nendsolid", "application/octet-stream")},
            data={
                "machine_uuid": machine_uuid,
                "process_uuid": process_uuid,
                "filament_uuids": f'["{filament_uuid}"]',
                "plate": "1",
            },
        )
        assert resp.status_code == 503
        assert "Too many active jobs" in resp.json()["detail"]
    finally:
        for i in range(main_mod.MAX_CONCURRENT_JOBS):
            main_mod.jobs.pop(f"pending-job-{i}", None)
        # Restore: leaving a real catalog installed would un-skip test_mock_contract.py's
        # "real" client parametrization in later-collected test files, which then fails
        # without an actual OrcaSlicer binary available.
        main_mod.catalog = original_catalog
