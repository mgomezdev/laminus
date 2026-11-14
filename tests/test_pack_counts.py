"""/api/pack `counts` (N copies per upload), instance cap, object naming, v1 discovery."""
import io
import os
import struct
import zipfile
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import app
from app.stl_to_3mf import inject_stls_into_3mf

client = TestClient(app)
BED = {"bed_x": "200", "bed_y": "200", "bed_z": "200"}


def _stl() -> bytes:
    buf = io.BytesIO()
    buf.write(b"counts-test".ljust(80))
    buf.write(struct.pack("<I", 1))
    for f in (0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0):
        buf.write(struct.pack("<f", f))
    buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def _files(*names):
    return [("files", (n, _stl(), "application/octet-stream")) for n in names]


@pytest.fixture
def pack_env(tmp_path, monkeypatch):
    """Capture what reaches inject; ARRANGE_DIR redirected so leftovers are visible."""
    arrange = tmp_path / "arrange"
    arrange.mkdir()
    monkeypatch.setattr(main_mod, "ARRANGE_DIR", str(arrange))
    captured = {"calls": 0}

    def _fake_inject(template_path, stl_paths, out_path, names=None):
        captured["calls"] += 1
        captured["paths"] = list(stl_paths)
        captured["names"] = None if names is None else list(names)
        captured["on_disk"] = sorted(os.listdir(os.path.dirname(stl_paths[0])))
        with open(out_path, "wb") as fh:
            fh.write(b"x")

    async def _slicer(*args, **kwargs):
        out = list(args)[list(args).index("--export-3mf") + 1]
        with open(out, "wb") as fh:
            fh.write(b"x")
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"Done\n", None))
        return proc

    with patch("app.main._inject_stls_into_3mf", new=_fake_inject), \
         patch("asyncio.create_subprocess_exec", new=_slicer):
        yield captured, arrange


def test_counts_repeat_paths_and_names(pack_env):
    cap, _ = pack_env
    r = client.post("/api/pack", files=_files("a.stl", "b.stl"), data={**BED, "counts": "[3,2]"})
    assert r.status_code == 200
    p = cap["paths"]
    assert len(p) == 5
    assert p[0] == p[1] == p[2] and p[3] == p[4] and p[0] != p[3]
    assert cap["names"] == ["a", "a", "a", "b", "b"]
    # each STL written exactly once (plus template + nothing else STL)
    assert [f for f in cap["on_disk"] if f.endswith(".stl")] == ["a.stl", "b.stl"]


def test_no_counts_is_one_each(pack_env):
    cap, _ = pack_env
    r = client.post("/api/pack", files=_files("a.stl", "b.stl"), data=BED)
    assert r.status_code == 200
    assert len(cap["paths"]) == 2 and len(set(cap["paths"])) == 2
    assert cap["names"] == ["a", "b"]


@pytest.mark.parametrize("bad", [
    "not json", '{"a": 1}', "5", "[1]", "[1,2,3]", "[0,1]", "[-1,1]", "[1.5,1]",
    "[true,1]", '["2",1]', "[null,1]",
])
def test_invalid_counts_rejected(pack_env, bad):
    cap, arrange = pack_env
    r = client.post("/api/pack", files=_files("a.stl", "b.stl"), data={**BED, "counts": bad})
    assert r.status_code == 422, r.text
    assert "counts" in r.json()["detail"]
    assert cap["calls"] == 0
    assert os.listdir(arrange) == []


def test_total_over_cap_rejected_and_at_cap_ok(pack_env, monkeypatch):
    cap, arrange = pack_env
    monkeypatch.setattr(main_mod, "PACK_MAX_INSTANCES", 5)
    r = client.post("/api/pack", files=_files("a.stl", "b.stl"), data={**BED, "counts": "[3,3]"})
    assert r.status_code == 422
    assert "Maximum 5 parts per pack request; got 6." in r.json()["detail"]
    assert cap["calls"] == 0 and os.listdir(arrange) == []
    r = client.post("/api/pack", files=_files("a.stl", "b.stl"), data={**BED, "counts": "[3,2]"})
    assert r.status_code == 200
    assert len(cap["paths"]) == 5


def test_same_filename_distinct_paths_same_name(pack_env):
    cap, _ = pack_env
    r = client.post("/api/pack", files=_files("bracket.stl", "bracket.stl"),
                    data={**BED, "counts": "[2,1]"})
    assert r.status_code == 200
    p = cap["paths"]
    assert p[0] == p[1] and p[2] != p[0]
    assert cap["names"] == ["bracket", "bracket", "bracket"]
    assert len({os.path.basename(x) for x in p}) == 2


def test_v1_capabilities_advertise_pack_counts():
    body = client.get("/api/v1").json()
    assert "pack.counts" in body["features"] and "pack.names" in body["features"]
    assert body["limits"]["max_pack_instances"] == main_mod.PACK_MAX_INSTANCES


def test_v1_pack_accepts_counts(pack_env):
    cap, _ = pack_env
    r = client.post("/api/v1/pack", files=_files("a.stl", "b.stl"), data={**BED, "counts": "[1,2]"})
    assert r.status_code == 200
    assert cap["names"] == ["a", "b", "b"]
    r = client.post("/api/v1/pack", files=_files("a.stl", "b.stl"), data={**BED, "counts": "[1]"})
    assert r.status_code == 422


def test_real_inject_names_and_structure(tmp_path):
    tpl = str(tmp_path / "t.3mf")
    with zipfile.ZipFile(tpl, "w") as zf:
        zf.writestr("Metadata/project_settings.config", "{}")
    a = tmp_path / "a.stl"
    a.write_bytes(_stl())
    b = tmp_path / "b.stl"
    b.write_bytes(_stl())
    out = str(tmp_path / "out.3mf")
    weird = 'we&ird "n<ame'
    inject_stls_into_3mf(tpl, [str(a)] * 3 + [str(b)], out, names=["a", "a", "a", weird])
    with zipfile.ZipFile(out) as zf:
        root = ET.fromstring(zf.read("3D/3dmodel.model"))
        obj_entries = [n for n in zf.namelist() if n.startswith("3D/Objects/")]
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    objs = root.findall("m:resources/m:object", ns)
    assert [o.get("name") for o in objs] == ["a", "a", "a", weird]
    assert len(root.findall("m:build/m:item", ns)) == 4
    assert len(obj_entries) == 4 and len(set(obj_entries)) == 4


def test_real_inject_default_names_are_path_stems(tmp_path):
    tpl = str(tmp_path / "t.3mf")
    with zipfile.ZipFile(tpl, "w") as zf:
        zf.writestr("Metadata/project_settings.config", "{}")
    a = tmp_path / "widget.stl"
    a.write_bytes(_stl())
    out = str(tmp_path / "out.3mf")
    inject_stls_into_3mf(tpl, [str(a), str(a)], out)
    with zipfile.ZipFile(out) as zf:
        root = ET.fromstring(zf.read("3D/3dmodel.model"))
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    assert [o.get("name") for o in root.findall("m:resources/m:object", ns)] == ["widget", "widget"]


def test_real_inject_parses_each_path_once(tmp_path):
    from unittest.mock import patch as _patch
    import app.stl_to_3mf as mod
    tpl = str(tmp_path / "t.3mf")
    with zipfile.ZipFile(tpl, "w") as zf:
        zf.writestr("Metadata/project_settings.config", "{}")
    a = tmp_path / "a.stl"
    a.write_bytes(_stl())
    with _patch.object(mod, "parse_stl_triangles", wraps=mod.parse_stl_triangles) as spy:
        inject_stls_into_3mf(tpl, [str(a)] * 4, str(tmp_path / "o.3mf"))
    assert spy.call_count == 1
