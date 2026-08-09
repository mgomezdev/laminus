"""Validation-failure edge cases for POST /api/slice/start that must return 422,
not an unhandled 500, before any OrcaSlicer subprocess is ever launched.

TestClient(app) without a `with` block does not run the FastAPI lifespan (see
test_mock_contract.py, where the same pattern leaves `catalog` as None and the
"real" client parametrization skips) — so it's safe to inject a real,
fixture-built ProfileCatalog directly onto app.main.catalog without it being
raced or clobbered by the background catalog-build task.
"""
from fastapi.testclient import TestClient

import app.main as main_mod
from app.profile_catalog import ProfileCatalog
from tests.conftest import write_json


def _catalog_and_uuids(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    data = cat.as_dict()
    machine = data["machine"][0]
    process = next(p for p in data["process"] if p["name"] == "0.20mm Standard @BBL X1E")
    filament = data["filament"][0]
    return cat, machine["uuid"], process["uuid"], filament["uuid"]


def test_slice_start_rejects_malformed_stl(profile_tree):
    """An STL that fails to parse (truncated/malformed) must 422, not 500
    (regression: struct.error / ValueError from _stl_to_3mf was unhandled)."""
    cat, machine_uuid, process_uuid, filament_uuid = _catalog_and_uuids(profile_tree)
    main_mod.catalog = cat
    client = TestClient(main_mod.app)
    resp = client.post(
        "/api/slice/start",
        files={"file": ("bad.stl", b"", "application/octet-stream")},
        data={
            "machine_uuid": machine_uuid,
            "process_uuid": process_uuid,
            "filament_uuids": f'["{filament_uuid}"]',
            "plate": "1",
        },
    )
    assert resp.status_code == 422
    assert "STL" in resp.json()["detail"]
