"""Regression: POST /api/v1/pack in UUID mode must 422 (not 500) when the resolved
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
        "/api/v1/pack",
        files=[("files", ("m.stl", b"solid t\nendsolid", "application/octet-stream"))],
        data={
            "machine_uuid": machine_uuid,
            "process_uuid": process_uuid,
            "filament_uuids": f'["{filament_uuid}"]',
        },
    )
    assert resp.status_code == 422
    assert machine_uuid in resp.json()["error"]["message"]


def _post_pack(client, **data):
    return client.post(
        "/api/v1/pack",
        files=[("files", ("m.stl", b"solid t\nendsolid", "application/octet-stream"))],
        data=data,
    )


def test_pack_machine_uuid_alone_rejects_machine_without_bed_dims(profile_tree):
    cat, machine_uuid, _, _ = _catalog_and_uuids(profile_tree)
    main_mod.catalog = cat
    resp = _post_pack(TestClient(main_mod.app), machine_uuid=machine_uuid)
    assert resp.status_code == 422
    assert "bed dimensions" in resp.json()["error"]["message"]


def test_pack_machine_uuid_alone_unknown_machine(profile_tree):
    cat, _, process_uuid, _ = _catalog_and_uuids(profile_tree)
    main_mod.catalog = cat
    client = TestClient(main_mod.app)
    assert _post_pack(client, machine_uuid="nope").status_code == 422
    # a non-machine UUID is not accepted either
    assert _post_pack(client, machine_uuid=process_uuid).status_code == 422


def test_pack_machine_uuid_alone_503_without_catalog(monkeypatch):
    monkeypatch.setattr(main_mod, "catalog", None)
    assert _post_pack(TestClient(main_mod.app), machine_uuid="x").status_code == 503


def test_pack_explicit_bed_wins_over_machine_uuid(profile_tree, monkeypatch):
    """bed_x/y/z present -> geometry mode; the machine (here malformed) is never consulted."""
    cat, machine_uuid, _, _ = _catalog_and_uuids(profile_tree)
    main_mod.catalog = cat
    resp = _post_pack(TestClient(main_mod.app), machine_uuid=machine_uuid,
                      bed_x="100", bed_y="100", bed_z="100")
    assert "bed dimensions" not in resp.text
