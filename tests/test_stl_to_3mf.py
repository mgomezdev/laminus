import os, struct, zipfile
from app.stl_to_3mf import stl_to_3mf

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
        assert any("3dmodel.model" in n for n in zf.namelist())
        assert "[Content_Types].xml" in zf.namelist()

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
