"""Regression: POST /api/pack in UUID mode must 422 (not 500) when the resolved
machine profile has no printable_area/printable_height, and must clean up the
job directory it already created.

Uses the same profile_tree-injection pattern as test_slice_start_validation.py.
"""
from fastapi.testclient import TestClient

import app.main as main_mod
from app.profile_catalog import ProfileCatalog


def _catalog_and_uuids(profile_tree):
    cat = ProfileCatalog(system_dir=profile_tree["system_dir"], user_dir=profile_tree["user_dir"])
    cat.build()
    data = cat.as_dict()
    # profile_tree's machine fixture defines bed_size_x/bed_size_y but no
    # printable_area/printable_height - exactly the malformed-machine-profile case.
    machine = data["machine"][0]
    process = next(p for p in data["process"] if p["name"] == "0.20mm Standard @BBL X1E")
    filament = data["filament"][0]
    return cat, machine["uuid"], process["uuid"], filament["uuid"]


def test_pack_uuid_mode_rejects_machine_without_bed_dims(profile_tree):
    cat, machine_uuid, process_uuid, filament_uuid = _catalog_and_uuids(profile_tree)
    main_mod.catalog = cat
    client = TestClient(main_mod.app)
    resp = client.post(
        "/api/pack",
        files=[("files", ("m.stl", b"solid t\nendsolid", "application/octet-stream"))],
        data={
            "machine_uuid": machine_uuid,
            "process_uuid": process_uuid,
            "filament_uuids": f'["{filament_uuid}"]',
        },
    )
    assert resp.status_code == 422
    assert machine_uuid in resp.json()["detail"]
