import json, os, struct, zipfile
from app.stl_to_3mf import stl_to_3mf, inject_stls_into_3mf

def _binary_stl(tmp_path, triangles=1):
    path = str(tmp_path / "cube.stl")
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)
        f.write(struct.pack("<I", triangles))
        for _ in range(triangles):
            f.write(struct.pack("<fff", 0, 0, 1))
            f.write(struct.pack("<fff", 0, 0, 0))
            f.write(struct.pack("<fff", 1, 0, 0))
            f.write(struct.pack("<fff", 0, 1, 0))
            f.write(struct.pack("<H", 0))
    return path

def _ascii_stl(tmp_path):
    path = str(tmp_path / "ascii.stl")
    with open(path, "w") as f:
        f.write("solid test\nfacet normal 0 0 1\n outer loop\n")
        f.write("  vertex 0 0 0\n  vertex 1 0 0\n  vertex 0 1 0\n")
        f.write(" endloop\nendfacet\nendsolid test\n")
    return path

def test_binary_stl_produces_valid_3mf(tmp_path):
    dst = str(tmp_path / "out.3mf")
    stl_to_3mf(_binary_stl(tmp_path), dst)
    with zipfile.ZipFile(dst) as zf:
        names = zf.namelist()
        assert "[Content_Types].xml" in names
        assert "_rels/.rels" in names
        assert "3D/3dmodel.model" in names
        xml = zf.read("3D/3dmodel.model").decode()
        assert "<vertex" in xml and "<triangle" in xml

def test_ascii_stl_produces_valid_3mf(tmp_path):
    dst = str(tmp_path / "out.3mf")
    stl_to_3mf(_ascii_stl(tmp_path), dst)
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
        assert "<vertex" in xml and "<triangle" in xml

def test_vertex_count_matches_triangles(tmp_path):
    dst = str(tmp_path / "out.3mf")
    stl_to_3mf(_binary_stl(tmp_path, triangles=2), dst)
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    assert xml.count("<vertex ") == 6

def _make_template_3mf(path: str, extra_settings: dict | None = None) -> str:
    """Write a minimal valid template 3MF with project_settings.config."""
    settings = {"nozzle_diameter": ["0.4"], "printable_area": ["0x0", "220x0", "220x220", "0x220"], "from": "user"}
    if extra_settings:
        settings.update(extra_settings)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        zf.writestr("Metadata/project_settings.config", json.dumps(settings))
    return path


def test_inject_single_stl_preserves_settings(tmp_path):
    template = _make_template_3mf(str(tmp_path / "tmpl.3mf"))
    stl = _binary_stl(tmp_path)
    out = str(tmp_path / "out.3mf")
    inject_stls_into_3mf(template, [stl], out)
    with zipfile.ZipFile(out) as zf:
        settings = json.loads(zf.read("Metadata/project_settings.config"))
    assert settings["nozzle_diameter"] == ["0.4"]
    assert settings["printable_area"] == ["0x0", "220x0", "220x220", "0x220"]


def test_inject_single_stl_structure(tmp_path):
    template = _make_template_3mf(str(tmp_path / "tmpl.3mf"))
    stl = _binary_stl(tmp_path)
    out = str(tmp_path / "out.3mf")
    inject_stls_into_3mf(template, [stl], out)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "3D/3dmodel.model" in names
        assert "3D/_rels/3dmodel.model.rels" in names
        obj_files = [n for n in names if n.startswith("3D/Objects/")]
        assert len(obj_files) == 1
        xml = zf.read("3D/3dmodel.model").decode()
        assert "<vertex" not in xml  # geometry lives in Objects/, not main model
        assert 'objectid="2"' in xml  # wrapper object


def test_inject_multiple_stls_creates_separate_objects(tmp_path):
    template = _make_template_3mf(str(tmp_path / "tmpl.3mf"))
    stls = [_binary_stl(tmp_path), _ascii_stl(tmp_path)]
    out = str(tmp_path / "out.3mf")
    inject_stls_into_3mf(template, stls, out)
    with zipfile.ZipFile(out) as zf:
        obj_files = [n for n in zf.namelist() if n.startswith("3D/Objects/")]
        assert len(obj_files) == 2
        rels = zf.read("3D/_rels/3dmodel.model.rels").decode()
        assert 'Id="rel-1"' in rels
        assert 'Id="rel-2"' in rels


def test_inject_missing_project_settings_raises(tmp_path):
    bad = str(tmp_path / "bad.3mf")
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("[Content_Types].xml", "")
        zf.writestr("_rels/.rels", "")
    import pytest
    with pytest.raises(ValueError, match="project_settings"):
        inject_stls_into_3mf(bad, [_binary_stl(tmp_path)], str(tmp_path / "out.3mf"))


def test_inject_object_files_contain_geometry(tmp_path):
    template = _make_template_3mf(str(tmp_path / "tmpl.3mf"))
    stl = _binary_stl(tmp_path, triangles=2)
    out = str(tmp_path / "out.3mf")
    inject_stls_into_3mf(template, [stl], out)
    with zipfile.ZipFile(out) as zf:
        obj_file = next(n for n in zf.namelist() if n.startswith("3D/Objects/"))
        xml = zf.read(obj_file).decode()
    assert xml.count("<vertex ") == 6   # 2 triangles × 3 vertices


def test_binary_stl_with_solid_header(tmp_path):
    """Binary STLs whose header starts with 'solid' must not be treated as ASCII."""
    path = str(tmp_path / "tricky.stl")
    with open(path, "wb") as f:
        header = b"solid fake model name" + b"\x00" * (80 - len(b"solid fake model name"))
        f.write(header)
        f.write(struct.pack("<I", 1))  # 1 triangle
        f.write(struct.pack("<fff", 0, 0, 1))  # normal
        f.write(struct.pack("<fff", 0, 0, 0))  # v1
        f.write(struct.pack("<fff", 1, 0, 0))  # v2
        f.write(struct.pack("<fff", 0, 1, 0))  # v3
        f.write(struct.pack("<H", 0))  # attr
    dst = str(tmp_path / "out.3mf")
    stl_to_3mf(path, dst)
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    assert xml.count("<vertex ") == 3
