"""/api/pack `items` (file + quantity pairs): validation, v1 error details, injector pass-through."""
import io
import json
import struct
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app, PACK_MAX_INSTANCES


def _stl() -> bytes:
    buf = io.BytesIO()
    buf.write(b"qty-test".ljust(80))
    buf.write(struct.pack("<I", 1))
    for f in (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0):
        buf.write(struct.pack("<f", f))
    buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def _post(client, items=None, names=("a.stl", "b.stl"), path="/api/pack"):
    data = {"bed_x": "200", "bed_y": "200", "bed_z": "200"}
    if items is not None:
        data["items"] = items if isinstance(items, str) else json.dumps(items)
    return client.post(
        path,
        files=[("files", (n, _stl(), "application/octet-stream")) for n in names],
        data=data,
    )


def _item(file, quantity):
    return {"file": file, "quantity": quantity}


BAD_ITEMS = {
    "not_json": "not json",
    "object_not_array": '{"a": 1}',
    "scalar": "5",
    "empty_array": "[]",                                   # no entry for either file
    "missing_file_entry": [_item("a.stl", 1)],
    "unknown_file": [_item("a.stl", 1), _item("zzz.stl", 1)],
    "duplicate_entry": [_item("a.stl", 1), _item("a.stl", 2)],
    "entry_not_object": ["a.stl", "b.stl"],
    "missing_quantity": [_item("a.stl", 1), {"file": "b.stl"}],
    "missing_file_key": [_item("a.stl", 1), {"quantity": 2}],
    "zero": [_item("a.stl", 0), _item("b.stl", 1)],
    "negative": [_item("a.stl", -1), _item("b.stl", 1)],
    "float": [_item("a.stl", 1.5), _item("b.stl", 1)],
    "bool": [_item("a.stl", True), _item("b.stl", 1)],
    "string_qty": [_item("a.stl", "2"), _item("b.stl", 1)],
    "over_cap": [_item("a.stl", PACK_MAX_INSTANCES), _item("b.stl", 1)],
    "sum_over_cap": [_item("a.stl", 250), _item("b.stl", 251)],
}


@pytest.mark.parametrize("items", BAD_ITEMS.values(), ids=BAD_ITEMS.keys())
def test_pack_rejects_bad_items(items):
    client = TestClient(app)
    assert _post(client, items).status_code == 422
    assert _post(client, items, path="/api/v1/pack").status_code == 422


def test_pack_rejects_duplicate_upload_filenames_when_items_given():
    resp = _post(TestClient(app), [_item("a.stl", 1), _item("a.stl", 1)], names=("a.stl", "a.stl"))
    assert resp.status_code == 422


def test_legacy_422_keeps_plain_string_detail():
    body = _post(TestClient(app), [_item("a.stl", 0), _item("b.stl", 1)]).json()
    assert isinstance(body["detail"], str)
    assert "must be >= 1" in body["detail"]


def test_v1_422_lists_each_problem_in_details():
    resp = _post(
        TestClient(app),
        [_item("a.stl", 0), _item("zzz.stl", 2), _item("b.stl", 1.5)],
        path="/api/v1/pack",
    )
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "validation_error" and err["status"] == 422
    by_loc = {tuple(d["loc"]): d for d in err["details"]}
    assert by_loc[("body", "items", 0, "quantity")]["msg"] == "must be >= 1"
    assert by_loc[("body", "items", 0, "quantity")]["input"] == 0
    assert by_loc[("body", "items", 1, "file")]["input"] == "zzz.stl"
    assert by_loc[("body", "items", 2, "quantity")]["msg"] == "must be an integer"


def test_v1_422_missing_entry_and_cap_context():
    client = TestClient(app)
    err = _post(client, [_item("a.stl", 1)], path="/api/v1/pack").json()["error"]
    assert any("no entry for uploaded file 'b.stl'" in d["msg"] for d in err["details"])

    err = _post(client, [_item("a.stl", 250), _item("b.stl", 251)], path="/api/v1/pack").json()["error"]
    assert err["details"][0]["ctx"] == {"total": 501, "max": PACK_MAX_INSTANCES}


def _run(items, names=("a.stl", "b.stl")):
    captured = {}

    def _fake_inject(template_path, stl_paths, out_path, quantities=None):
        captured["quantities"] = quantities
        with open(out_path, "wb") as fh:
            fh.write(b"x")

    async def _mock_slicer_ok(*args, **kwargs):
        out_path = list(args)[list(args).index("--export-3mf") + 1]
        with open(out_path, "wb") as fh:
            fh.write(b"x")
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"Done\n", None))
        return proc

    with patch("app.main._inject_stls_into_3mf", new=_fake_inject), \
         patch("asyncio.create_subprocess_exec", new=_mock_slicer_ok):
        resp = _post(TestClient(app), items, names=names)
    return resp, captured


def test_pack_maps_items_to_upload_order():
    # manifest order differs from upload order; quantities must follow the uploads
    resp, captured = _run([_item("b.stl", 1), _item("a.stl", 3)])
    assert resp.status_code == 200
    assert captured["quantities"] == [3, 1]


def test_pack_without_items_is_legacy():
    resp, captured = _run(None)
    assert resp.status_code == 200
    assert captured["quantities"] is None
