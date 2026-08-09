"""Download behavior for jobs restored as stubs after a Laminus restart
(see _load_jobs_on_startup / the `_stub` flag in app/main.py)."""
from fastapi.testclient import TestClient

import app.main as main_mod


def test_download_stub_job_returns_404_not_misleading_400():
    """A stub job's disk files are gone; downloading it must say so clearly (404),
    not claim the job "is not complete" while also reporting status "completed"."""
    client = TestClient(main_mod.app)
    job_id = "stub-job-1"
    main_mod.jobs[job_id] = {
        "id": job_id, "status": "completed", "error": None,
        "sliced_file": None, "logger": None, "created_at": 0,
        "_wall_created_at": 0, "_stub": True,
    }
    try:
        resp = client.get(f"/api/slice/download/{job_id}")
        assert resp.status_code == 404
    finally:
        main_mod.jobs.pop(job_id, None)


def test_logs_stub_job_returns_404_not_attribute_error():
    """A stub job has `logger: None`; streaming its logs must 404, not raise
    AttributeError from calling .get_stream() on None."""
    client = TestClient(main_mod.app)
    job_id = "stub-job-2"
    main_mod.jobs[job_id] = {
        "id": job_id, "status": "completed", "error": None,
        "sliced_file": None, "logger": None, "created_at": 0,
        "_wall_created_at": 0, "_stub": True,
    }
    try:
        resp = client.get(f"/api/slice/logs/{job_id}")
        assert resp.status_code == 404
    finally:
        main_mod.jobs.pop(job_id, None)
