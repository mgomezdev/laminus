import json, os, struct, zipfile
import pytest
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


def test_inject_quantities_share_one_mesh(tmp_path):
    template = _make_template_3mf(str(tmp_path / "tmpl.3mf"))
    stls = [_binary_stl(tmp_path), _ascii_stl(tmp_path)]
    out = str(tmp_path / "out.3mf")
    inject_stls_into_3mf(template, stls, out, [3, 1])
    with zipfile.ZipFile(out) as zf:
        obj_files = [n for n in zf.namelist() if n.startswith("3D/Objects/")]
        assert len(obj_files) == 2
        xml = zf.read("3D/3dmodel.model").decode()
    assert xml.count("<item ") == 4
    assert xml.count("<component ") == 4
    assert xml.count("cube_1.model") == 3
    assert xml.count("ascii_2.model") == 1


def test_inject_object_ids_unique_per_stl(tmp_path):
    # OrcaSlicer collapses components that share objectid across different mesh files
    # onto the first mesh (distinct STLs came out as copies of the first one).
    template = _make_template_3mf(str(tmp_path / "tmpl.3mf"))
    stls = [_binary_stl(tmp_path), _ascii_stl(tmp_path)]
    out = str(tmp_path / "out.3mf")
    inject_stls_into_3mf(template, stls, out, [2, 1])
    with zipfile.ZipFile(out) as zf:
        main = zf.read("3D/3dmodel.model").decode()
        assert 'p:path="/3D/Objects/cube_1.model" objectid="1"' in main
        assert 'p:path="/3D/Objects/ascii_2.model" objectid="2"' in main
        assert '<object id="1"' in zf.read("3D/Objects/cube_1.model").decode()
        assert '<object id="2"' in zf.read("3D/Objects/ascii_2.model").decode()


def test_inject_quantities_length_mismatch_raises(tmp_path):
    template = _make_template_3mf(str(tmp_path / "tmpl.3mf"))
    with pytest.raises(ValueError, match="quantities"):
        inject_stls_into_3mf(template, [_binary_stl(tmp_path)], str(tmp_path / "o.3mf"), [1, 2])


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


def test_ascii_stl_with_zero_triangles_raises(tmp_path):
    """An ASCII STL that parses but matches no triangles (e.g. uppercase VERTEX,
    a case-sensitive miss in _parse_ascii) must raise, not silently produce a
    structurally-valid but empty 3MF that slices into empty gcode."""
    path = str(tmp_path / "uppercase.stl")
    with open(path, "w") as f:
        f.write("solid test\nfacet normal 0 0 1\n outer loop\n")
        f.write("  VERTEX 0 0 0\n  VERTEX 1 0 0\n  VERTEX 0 1 0\n")
        f.write(" endloop\nendfacet\nendsolid test\n")
    with pytest.raises(ValueError, match="[Nn]o triangles"):
        stl_to_3mf(path, str(tmp_path / "out.3mf"))


def test_misclassified_binary_stl_with_zero_triangles_raises(tmp_path):
    """A binary STL whose header starts with 'solid' but whose size doesn't match
    the binary-format size formula is misclassified as ASCII by _is_binary; parsing
    its binary bytes as ASCII text yields zero "vertex" lines. Must raise, not
    silently produce an empty 3MF."""
    path = str(tmp_path / "tricky_mismatched_size.stl")
    with open(path, "wb") as f:
        header = b"solid weird binary export" + b"\x00" * (80 - len(b"solid weird binary export"))
        f.write(header)
        f.write(struct.pack("<I", 1))  # claims 1 triangle (expects 80+4+50=134 bytes)
        f.write(b"\x00" * 10)  # actual size doesn't match -> misclassified as ASCII
    with pytest.raises(ValueError, match="[Nn]o triangles"):
        stl_to_3mf(path, str(tmp_path / "out.3mf"))


def test_inject_raises_when_one_stl_has_zero_triangles(tmp_path):
    template = _make_template_3mf(str(tmp_path / "tmpl.3mf"))
    empty_stl = str(tmp_path / "empty.stl")
    with open(empty_stl, "w") as f:
        f.write("solid empty\nendsolid empty\n")
    with pytest.raises(ValueError, match="[Nn]o triangles"):
        inject_stls_into_3mf(template, [empty_stl], str(tmp_path / "out.3mf"))


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


def test_stl_min_z_and_restore_build_item_z(tmp_path):
    import struct
    import zipfile
    from app.stl_to_3mf import restore_build_item_z, stl_min_z

    stl = tmp_path / "p.stl"
    tri = [(0, 0, 5.0), (1, 0, 7.0), (0, 1, 9.0)]
    stl.write_bytes(b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<3f", 0, 0, 1)
                    + b"".join(struct.pack("<3f", *v) for v in tri) + b"\0\0")
    assert stl_min_z(str(stl)) == 5.0

    model = ('<model><build>'
             '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 10 20 0"/>'
             '<item objectid="4" printable="1" transform="0 1 0 -1 0 0 0 0 1 30 40 0.5"/>'
             '</build></model>')
    # Orca reorders objects: item 4 came from input instance 0, item 2 from instance 1
    cfg = ('<config>'
           '<object id="4"><part id="1"><metadata key="source_object_id" value="0"/></part></object>'
           '<object id="2"><part id="1"><metadata key="source_object_id" value="1"/></part></object>'
           '</config>')
    packed = tmp_path / "packed.3mf"
    with zipfile.ZipFile(packed, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", cfg)
    restore_build_item_z(str(packed), [5.0, -10.0])
    with zipfile.ZipFile(packed) as z:
        out = z.read("3D/3dmodel.model").decode()
        assert z.read("Metadata/model_settings.config").decode() == cfg
    assert 'transform="1 0 0 0 1 0 0 0 1 10 20 -10.0"' in out   # objectid 2 = instance 1
    assert 'transform="0 1 0 -1 0 0 0 0 1 30 40 5.5"' in out    # objectid 4 = instance 0

    with pytest.raises(ValueError):
        restore_build_item_z(str(packed), [1.0])


def _box_3mf(path, objs):
    """objs: [(objectid, name, (sx, sy, sz), tz)] -> minimal 3MF; box spans 0..s, item shifted by tz."""
    import zipfile
    res, items, cfg = [], [], []
    for oid, name, (sx, sy, sz), tz in objs:
        v = [(x, y, z) for x in (0, sx) for y in (0, sy) for z in (0, sz)]
        tri = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
               (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
        res.append(f'<object id="{oid}"><mesh><vertices>'
                   + "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in v)
                   + '</vertices><triangles>'
                   + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tri)
                   + '</triangles></mesh></object>')
        items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 0 0 {tz}"/>')
        cfg.append(f'<object id="{oid}"><metadata key="name" value="{name}"/></object>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", f'<model><resources>{"".join(res)}</resources><build>{"".join(items)}</build></model>')
        z.writestr("Metadata/model_settings.config", f'<config>{"".join(cfg)}</config>')


def test_restore_z_from_source_matches_by_name_and_shape(tmp_path):
    import zipfile
    from app.stl_to_3mf import restore_z_from_source

    src, out = str(tmp_path / "src.3mf"), str(tmp_path / "out.3mf")
    _box_3mf(src, [("1", "tall", (10, 10, 40), 3.0), ("2", "wide", (30, 30, 5), -2.0)])
    # Orca-like output: objects swapped/renumbered and re-seated at min Z 0
    _box_3mf(out, [("7", "wide", (30, 30, 5), 0.0), ("9", "tall", (10, 10, 40), 0.0)])
    restore_z_from_source(src, out)
    model = zipfile.ZipFile(out).read("3D/3dmodel.model").decode()
    assert 'objectid="7" transform="1 0 0 0 1 0 0 0 1 0 0 -2.0"' in model
    assert 'objectid="9" transform="1 0 0 0 1 0 0 0 1 0 0 3.0"' in model

    _box_3mf(out, [("7", "other", (30, 30, 5), 0.0)])
    with pytest.raises(ValueError):
        restore_z_from_source(src, out)


def test_restore_z_rejects_entity_declarations(tmp_path):
    import zipfile
    from app.stl_to_3mf import restore_z_from_source

    src, out = str(tmp_path / "src.3mf"), str(tmp_path / "out.3mf")
    _box_3mf(out, [("1", "a", (10, 10, 10), 0.0)])
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("3D/3dmodel.model",
                   '<?xml version="1.0"?><!DOCTYPE m [<!ENTITY x "boom">]><model><resources/><build/></model>')
    with pytest.raises(ValueError):  # defusedxml's exceptions subclass ValueError
        restore_z_from_source(src, out)
