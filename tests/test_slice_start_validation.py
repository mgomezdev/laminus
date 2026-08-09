"""Validation-failure edge cases for POST /api/slice/start that must return 422,
not an unhandled 500, before any OrcaSlicer subprocess is ever launched.

TestClient(app) without a `with` block does not run the FastAPI lifespan (see
test_mock_contract.py, where the same pattern leaves `catalog` as None and the
"real" client parametrization skips) — so it's safe to inject a real,
fixture-built ProfileCatalog directly onto app.main.catalog without it being
raced or clobbered by the background catalog-build task.
"""
import pytest
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


def test_slice_start_rejects_malformed_3mf(profile_tree):
    """A .3mf upload that isn't a valid ZIP (corrupt, truncated, zero-byte) must 422,
    not 500 (regression: zipfile.BadZipFile from embed_project_settings was unhandled)."""
    cat, machine_uuid, process_uuid, filament_uuid = _catalog_and_uuids(profile_tree)
    main_mod.catalog = cat
    client = TestClient(main_mod.app)
    resp = client.post(
        "/api/slice/start",
        files={"file": ("bad.3mf", b"not a zip file", "application/octet-stream")},
        data={
            "machine_uuid": machine_uuid,
            "process_uuid": process_uuid,
            "filament_uuids": f'["{filament_uuid}"]',
            "plate": "1",
        },
    )
    assert resp.status_code == 422
    assert "3MF" in resp.json()["detail"]


def test_slice_start_rejects_zero_byte_3mf(profile_tree):
    cat, machine_uuid, process_uuid, filament_uuid = _catalog_and_uuids(profile_tree)
    main_mod.catalog = cat
    client = TestClient(main_mod.app)
    resp = client.post(
        "/api/slice/start",
        files={"file": ("empty.3mf", b"", "application/octet-stream")},
        data={
            "machine_uuid": machine_uuid,
            "process_uuid": process_uuid,
            "filament_uuids": f'["{filament_uuid}"]',
            "plate": "1",
        },
    )
    assert resp.status_code == 422


ESCAPING_EXPORT_NAMES = [
    "/config/user/default/machine/evil.json",
    "../../../data/jobs.json",
    "..\\..\\data\\jobs.json",
]


@pytest.mark.parametrize("export_name", ESCAPING_EXPORT_NAMES)
def test_slice_start_rejects_escaping_export_3mf(profile_tree, export_name):
    """export_3mf is passed to OrcaSlicer as --export-3mf and joined onto output_dir.
    An absolute path replaces output_dir outright, so an unvalidated value writes
    anywhere on disk — including the scanned profile volume and /data/jobs.json."""
    cat, machine_uuid, process_uuid, filament_uuid = _catalog_and_uuids(profile_tree)
    main_mod.catalog = cat
    client = TestClient(main_mod.app)
    resp = client.post(
        "/api/slice/start",
        files={"file": ("model.3mf", b"not a zip file", "application/octet-stream")},
        data={
            "machine_uuid": machine_uuid,
            "process_uuid": process_uuid,
            "filament_uuids": f'["{filament_uuid}"]',
            "plate": "1",
            "export_3mf": export_name,
        },
    )
    # Rejected before the upload is even parsed, so this cannot be the 3MF-parse 422.
    assert resp.status_code == 422
    assert "3MF" not in resp.json()["detail"]


@pytest.mark.parametrize("export_name", ESCAPING_EXPORT_NAMES)
def test_slice_prepared_rejects_escaping_export_3mf(export_name):
    """/api/slice/prepared takes the same export_3mf field and needs the same guard."""
    client = TestClient(main_mod.app)
    resp = client.post(
        "/api/slice/prepared",
        files={"file": ("model.3mf", b"not a zip file", "application/octet-stream")},
        data={"plate": "1", "export_3mf": export_name},
    )
    assert resp.status_code == 422
    assert "export_3mf" in resp.json()["detail"] or "Filename" in resp.json()["detail"]
