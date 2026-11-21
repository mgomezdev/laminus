"""API v1 contract: envelope, pagination, idempotency, discovery, legacy deprecation."""
import asyncio
import time
from collections import OrderedDict
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main_mod
from app import api_v1
from app.profile_catalog import ProfileCatalog

client = TestClient(main_mod.app)


@pytest.fixture(autouse=True)
def _clean_state():
    jobs, hist, cat = dict(main_mod.jobs), OrderedDict(main_mod.job_history), main_mod.catalog
    main_mod.jobs.clear()
    main_mod.job_history.clear()
    yield
    main_mod.jobs.clear()
    main_mod.jobs.update(jobs)
    main_mod.job_history.clear()
    main_mod.job_history.update(hist)
    main_mod.catalog = cat


@pytest.fixture
def cat(profile_tree):
    c = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    c.build()
    main_mod.catalog = c
    return c


def _hist(i, status="completed"):
    main_mod.job_history[f"j{i}"] = {"id": f"j{i}", "filename": f"f{i}.stl", "status": status,
                                     "output_format": "gcode", "sliced_file": None, "error": None,
                                     "created_at": 1000 + i, "updated_at": 1000 + i, "evicted": True}


def test_capabilities():
    r = client.get("/api/v1")
    assert r.status_code == 200
    body = r.json()
    assert body["api_version"] == "v1" and body["supported_versions"] == ["v1"]
    assert {"jobs", "jobs.idempotency", "pagination.cursor"} <= set(body["features"])
    assert body["limits"]["max_page_size"] == 200
    assert r.headers["API-Version"] == "v1" and r.headers["X-Request-ID"]


def test_request_id_echoed():
    r = client.get("/api/v1", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


def test_error_envelope_on_v1_and_retired_legacy_path():
    v1 = client.get("/api/v1/jobs/nope")
    assert v1.status_code == 404
    assert v1.json()["error"] == {"code": "job_not_found", "message": "Job not found.", "status": 404}
    assert v1.json()["request_id"] == v1.headers["X-Request-ID"]
    legacy = client.get("/api/slice/status/nope")
    assert legacy.status_code == 404 and legacy.json() == {"detail": "Not Found"}


def test_validation_errors_are_enveloped():
    r = client.get("/api/v1/jobs?limit=0")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["details"]


def test_job_pagination_walks_all_pages_newest_first():
    for i in range(5):
        _hist(i)
    seen, cursor = [], None
    for _ in range(4):
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = client.get("/api/v1/jobs", params=params).json()
        seen += [j["id"] for j in body["data"]]
        cursor = body["page"]["next_cursor"]
        if cursor is None:
            break
    assert seen == ["j4", "j3", "j2", "j1", "j0"]


def test_pagination_is_stable_when_new_jobs_arrive():
    for i in range(4):
        _hist(i)
    first = client.get("/api/v1/jobs?limit=2").json()
    _hist(99)  # newest; must not shift page 2
    second = client.get("/api/v1/jobs", params={"limit": 2, "cursor": first["page"]["next_cursor"]}).json()
    assert [j["id"] for j in second["data"]] == ["j1", "j0"]


def test_bad_cursor_is_422():
    r = client.get("/api/v1/jobs", params={"cursor": "%%%"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor"


def test_job_status_filter_and_view_shape():
    _hist(1, "failed")
    _hist(2)
    data = client.get("/api/v1/jobs?status=failed").json()["data"]
    assert [j["id"] for j in data] == ["j1"]
    assert data[0]["links"]["events"] == "/api/v1/jobs/j1/events"
    assert data[0]["artifact"]["available"] is False


def test_artifact_states():
    assert client.get("/api/v1/jobs/x/artifact").json()["error"]["code"] == "job_not_found"
    main_mod.jobs["p"] = {"id": "p", "status": "slicing", "error": None, "sliced_file": None, "logger": None}
    assert client.get("/api/v1/jobs/p/artifact").status_code == 409
    _hist(7)  # evicted: still listed, artifact gone
    assert client.get("/api/v1/jobs/j7").status_code == 200
    assert client.get("/api/v1/jobs/j7/artifact").json()["error"]["code"] == "artifact_gone"


def test_events_are_typed_and_resumable():
    async def build():
        lg = main_mod.JobLogger("e1")
        for line in ("one", "two", "__COMPLETED__"):
            res = lg.log(line)
            if asyncio.iscoroutine(res):
                await res
        return lg

    lg = asyncio.new_event_loop().run_until_complete(build())
    main_mod.jobs["e1"] = {"id": "e1", "status": "completed", "error": None, "sliced_file": None, "logger": lg}
    body = client.get("/api/v1/jobs/e1/events").text
    assert 'id: 1\nevent: log\ndata: {"message": "one"}' in body
    assert "event: completed" in body
    resumed = client.get("/api/v1/jobs/e1/events", headers={"Last-Event-ID": "2"}).text
    assert "one" not in resumed and "two" not in resumed and "event: completed" in resumed


def test_profiles_list_filter_paginate_detail(cat):
    body = client.get("/api/v1/profiles", params={"limit": 2}).json()
    assert len(body["data"]) == 2 and body["page"]["next_cursor"]
    machines = client.get("/api/v1/profiles", params={"limit": 200, "type": "machine"}).json()["data"]
    assert {p["type"] for p in machines} == {"machine"}
    uid = machines[0]["uuid"]
    assert client.get(f"/api/v1/profiles/{uid}").json()["uuid"] == uid
    assert client.get("/api/v1/profiles/none").json()["error"]["code"] == "profile_not_found"
    assert client.get("/api/v1/profiles?manufacturer=x").json()["error"]["code"] == "partial_machine_filter"
    assert client.get("/api/v1/profiles?type=bogus").status_code == 422


def test_profiles_catalog_not_ready_has_retry_after():
    main_mod.catalog = None
    r = client.get("/api/v1/profiles")
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert r.json()["error"]["code"] == "catalog_not_ready"






# ------------------------------------------------------------ idempotency

def _fake_start(calls):
    async def fake(**kw):
        calls.append(kw)
        jid = f"new{len(calls)}"
        main_mod.jobs[jid] = {"id": jid, "status": "pending", "error": None, "sliced_file": None,
                              "logger": None, "output_format": "gcode"}
        main_mod._history_upsert(jid, filename="a.stl", status="pending", output_format="gcode",
                                 sliced_file=None, error=None, evicted=False,
                                 created_at=time.time(), updated_at=time.time())
        return {"job_id": jid}
    return fake


def _post(headers=None, data=None, content=b"stl"):
    d = {"process_uuid": "p", "filament_uuids": '["f"]', "plate": "1"}
    d.update(data or {})
    return client.post("/api/v1/jobs", files={"file": ("a.stl", content)}, data=d, headers=headers or {})


def test_create_job_returns_202_location():
    calls = []
    with patch.object(main_mod, "start_slice", _fake_start(calls)):
        r = _post()
    assert r.status_code == 202 and r.headers["Location"] == "/api/v1/jobs/new1"
    assert r.json()["id"] == "new1" and r.json()["status"] == "pending"


def test_idempotency_replay_same_body_one_job():
    calls = []
    with patch.object(main_mod, "start_slice", _fake_start(calls)):
        a = _post({"Idempotency-Key": "k1"})
        b = _post({"Idempotency-Key": "k1"})
    assert len(calls) == 1
    assert a.json()["id"] == b.json()["id"] and b.headers["Idempotent-Replay"] == "true"


def test_idempotency_key_reuse_with_different_body_is_422():
    calls = []
    with patch.object(main_mod, "start_slice", _fake_start(calls)):
        _post({"Idempotency-Key": "k2"})
        r = _post({"Idempotency-Key": "k2"}, content=b"other")
        r2 = _post({"Idempotency-Key": "k2"}, data={"plate": "2"})
    assert r.status_code == r2.status_code == 422
    assert r.json()["error"]["code"] == "idempotency_key_reuse" and len(calls) == 1


def test_failed_create_does_not_burn_the_key():
    async def boom(**kw):
        raise HTTPException(status_code=422, detail="bad stl")

    with patch.object(main_mod, "start_slice", boom):
        r = _post({"Idempotency-Key": "k3"})
    assert r.status_code == 422 and r.json()["error"]["message"] == "bad stl"
    calls = []
    with patch.object(main_mod, "start_slice", _fake_start(calls)):
        assert _post({"Idempotency-Key": "k3"}).status_code == 202


def test_invalid_idempotency_key():
    assert _post({"Idempotency-Key": "x" * 300}).json()["error"]["code"] == "invalid_idempotency_key"


# ------------------------------------------------------------ projects + policy

def test_project_routes_exist_under_v1_with_enveloped_errors():
    r = client.post("/api/v1/projects/subset", files={"file": ("a.txt", b"x")}, data={"selection": "[]"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    r = client.post("/api/v1/projects/inspect", files={"file": ("a.txt", b"x")})
    assert "error" in r.json()






def test_v1_health_and_literal_profile_paths_beat_uuid_route():
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.headers["API-Version"] == "v1"
    r = client.get("/api/v1/profiles/broken")
    assert r.status_code != 404 or r.json()["error"]["code"] != "profile_not_found"
