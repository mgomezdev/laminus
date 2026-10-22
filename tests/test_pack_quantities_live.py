"""Live /api/pack `quantities` tests against a running Laminus (real OrcaSlicer).

Skipped unless LAMINUS_URL is set, e.g.:
    docker compose up -d && LAMINUS_URL=http://localhost:5000 pytest tests/test_pack_quantities_live.py
"""
import io
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

import pytest
import requests

BASE = os.environ.get("LAMINUS_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="LAMINUS_URL not set (needs a live Laminus + OrcaSlicer)")

_CUBE_TRIS = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
              (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
_PNS = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"


def cube_stl(size: float) -> bytes:
    verts = [(x, y, z) for x in (0, size) for y in (0, size) for z in (0, size)]
    buf = io.BytesIO()
    buf.write(b"\0" * 80)
    buf.write(struct.pack("<I", len(_CUBE_TRIS)))
    for tri in _CUBE_TRIS:
        buf.write(struct.pack("<3f", 0, 0, 0))
        for i in tri:
            buf.write(struct.pack("<3f", *verts[i]))
        buf.write(b"\0\0")
    return buf.getvalue()


def pack(stls, quantities=None, bed=(100, 100, 100), timeout=150):
    """stls: list of (filename, bytes). Returns the requests.Response."""
    files = [("files", (n, b, "application/octet-stream")) for n, b in stls]
    data = {"bed_x": bed[0], "bed_y": bed[1], "bed_z": bed[2]}
    if quantities is not None:
        data["quantities"] = quantities
    return requests.post(f"{BASE}/api/pack", files=files, data=data, timeout=timeout)


def analyse(content: bytes):
    """Return (instances_by_edge_size, n_plates) from a packed 3MF.

    Edge size is read from the referenced mesh's vertex extent, so instances are
    identified by geometry rather than by Orca's object naming.
    """
    z = zipfile.ZipFile(io.BytesIO(content))
    root = ET.fromstring(z.read("3D/3dmodel.model"))
    meshes = {}  # path -> edge size

    def mesh_edge(path):
        if path not in meshes:
            obj = ET.fromstring(z.read(path.lstrip("/")))
            xs = [float(v.get("x")) for v in obj.iter("{%s}vertex" % _NS["m"])]
            meshes[path] = round(max(xs) - min(xs))
        return meshes[path]

    objects = {}
    for o in root.find("m:resources", _NS).findall("m:object", _NS):
        comp = o.find("m:components/m:component", _NS)
        if comp is not None:
            objects[o.get("id")] = mesh_edge(comp.get(_PNS + "path"))
        else:
            xs = [float(v.get("x")) for v in o.iter("{%s}vertex" % _NS["m"])]
            objects[o.get("id")] = round(max(xs) - min(xs))

    counts: dict[int, int] = {}
    for item in root.find("m:build", _NS).findall("m:item", _NS):
        edge = objects[item.get("objectid")]
        counts[edge] = counts.get(edge, 0) + 1

    settings = z.read("Metadata/model_settings.config").decode()
    n_plates = len(re.findall(r"<plate>", settings))
    return counts, n_plates


@pytest.fixture(scope="module", autouse=True)
def _live():
    try:
        r = requests.get(f"{BASE}/api/health", timeout=5)
        assert r.json().get("orcaslicer_installed")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"Laminus not reachable at {BASE}: {e}")


A, B = ("a.stl", cube_stl(40)), ("b.stl", cube_stl(20))


# ---- positive ---------------------------------------------------------------

def test_acceptance_3_of_a_and_1_of_b():
    r = pack([A, B], "[3,1]")
    assert r.status_code == 200, r.text
    counts, _ = analyse(r.content)
    assert counts == {40: 3, 20: 1}


def test_copies_spill_onto_multiple_plates():
    # 60x60 bed fits exactly one 40mm cube per plate.
    r = pack([A], "[3]", bed=(60, 60, 100))
    assert r.status_code == 200, r.text
    counts, plates = analyse(r.content)
    assert counts == {40: 3}
    assert plates == 3


def test_single_file_high_quantity():
    r = pack([B], "[12]")
    assert r.status_code == 200, r.text
    counts, _ = analyse(r.content)
    assert counts == {20: 12}


def test_quantities_all_ones_matches_legacy():
    r1 = pack([A, B], "[1,1]")
    r2 = pack([A, B])
    assert r1.status_code == r2.status_code == 200
    assert analyse(r1.content)[0] == analyse(r2.content)[0] == {40: 1, 20: 1}


def test_legacy_without_quantities_unchanged():
    r = pack([A, B])
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {40: 1, 20: 1}


def test_legacy_duplicate_filenames_still_two_instances():
    r = pack([A, A])
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {40: 2}


def test_quantities_with_duplicate_filenames():
    r = pack([A, A], "[2,1]")
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {40: 3}


def test_total_at_cap_is_accepted():
    r = pack([B], "[500]", bed=(256, 256, 256), timeout=300)
    assert r.status_code == 200, r.text
    assert analyse(r.content)[0] == {20: 500}


# ---- negative ---------------------------------------------------------------

@pytest.mark.parametrize("quantities", [
    "[1]",            # too few
    "[1,1,1]",        # too many
    "[]",             # empty
    "[0,1]",          # zero
    "[-1,1]",         # negative
    "[1.5,1]",        # non-integer
    "[true,1]",       # bool
    '["2",1]',        # string
    "not json",
    '{"a":1}',
    "5",              # scalar
    "[501,1]",        # over total cap
    "[250,251]",      # sum over cap
])
def test_bad_quantities_rejected_422(quantities):
    r = pack([A, B], quantities)
    assert r.status_code == 422, (quantities, r.status_code, r.text)


def test_too_many_unique_files_still_422():
    r = pack([(f"p{i}.stl", cube_stl(10)) for i in range(51)])
    assert r.status_code == 422


def test_rejection_does_not_leak_work_dirs():
    before = requests.get(f"{BASE}/api/health", timeout=5).json().get("active_jobs")
    pack([A, B], "[0,1]")
    assert requests.get(f"{BASE}/api/health", timeout=5).json().get("active_jobs") == before
