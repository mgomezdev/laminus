"""Live /api/pack placement checks against a running Laminus (real OrcaSlicer).

Every packed instance must (a) sit inside its plate's bed footprint, (b) keep the input
model's geometry (rigid transform only), and (c) keep the input model's Z: a model whose
min Z is not 0 in the input (e.g. a mini floating above or below the plate by design) must
come back with the same min Z, not be re-seated. Parts may rotate about Z only; tilting
about X/Y would change layer-line direction.

Skipped unless LAMINUS_URL is set (see test_pack_quantities_live.py).
"""
import io
import json
import math
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

import httpx
import numpy as np
import pytest

BASE = os.environ.get("LAMINUS_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="LAMINUS_URL not set (needs a live Laminus + OrcaSlicer)")

_CUBE_TRIS = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
              (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_PNS = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
PLATE_GAP = 1.2  # Orca lays plates out on a grid with 20% spacing
TOL = 1e-3


class Part:
    """A test input: an axis-aligned box STL with its vertices kept for comparison."""

    def __init__(self, name, x, y, z0, z1):
        self.name = name
        self.verts = np.array([(a, b, c) for a in (0, x) for b in (0, y) for c in (z0, z1)], float)
        buf = io.BytesIO()
        buf.write(b"\0" * 80)
        buf.write(struct.pack("<I", len(_CUBE_TRIS)))
        for tri in _CUBE_TRIS:
            buf.write(struct.pack("<3f", 0, 0, 0))
            for i in tri:
                buf.write(struct.pack("<3f", *self.verts[i]))
            buf.write(b"\0\0")
        self.stl = buf.getvalue()


def pack(parts, qtys, bed):
    items = json.dumps([{"file": p.name, "quantity": q} for p, q in zip(parts, qtys)])
    r = httpx.post(
        f"{BASE}/api/v1/pack",
        files=[("files", (p.name, p.stl, "application/octet-stream")) for p in parts],
        data={"bed_x": bed[0], "bed_y": bed[1], "bed_z": bed[2], "items": items},
        timeout=300,
    )
    assert r.status_code == 200, r.text
    return r.content


def _shape_signature(v: np.ndarray) -> np.ndarray:
    """Rigid-motion invariant: sorted vertex distances from the centroid."""
    v = np.unique(np.round(v, 4), axis=0)  # the 3MF writer emits unindexed (per-triangle) vertices
    return np.sort(np.linalg.norm(v - v.mean(axis=0), axis=1))


def placements(content: bytes):
    """Per instance, in input order: (world_vertices, tilted: bool, plate_index).

    Orca reorders build items; the input position comes from `source_object_id`."""
    z = zipfile.ZipFile(io.BytesIO(content))
    root = ET.fromstring(z.read("3D/3dmodel.model"))

    meshes: dict[str, np.ndarray] = {}

    def mesh(path):
        if path not in meshes:
            obj = ET.fromstring(z.read(path.lstrip("/")))
            meshes[path] = np.array(
                [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in obj.iter(_NS + "vertex")]
            )
        return meshes[path]

    comp_path = {}
    for o in root.find(_NS + "resources").findall(_NS + "object"):
        c = o.find(f"{_NS}components/{_NS}component")
        comp_path[o.get("id")] = c.get(_PNS + "path")

    cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    part_matrix, source_of = {}, {}
    for o in cfg.findall("object"):
        source_of[o.get("id")] = int([md.get("value") for md in o.findall("part/metadata")
                                      if md.get("key") == "source_object_id"][0])
        m = [md.get("value") for md in o.findall("part/metadata") if md.get("key") == "matrix"]
        part_matrix[o.get("id")] = np.array(m[0].split(), float).reshape(4, 4) if m else np.eye(4)
    plate_of = {}
    plates = cfg.findall("plate")
    for k, plate in enumerate(plates):
        for inst in plate.findall("model_instance"):
            oid = [md.get("value") for md in inst.findall("metadata") if md.get("key") == "object_id"][0]
            assert oid not in plate_of, f"object {oid} assigned to more than one plate"
            plate_of[oid] = k

    out = []
    for item in root.find(_NS + "build").findall(_NS + "item"):
        oid = item.get("objectid")
        t = np.array(item.get("transform").split(), float)
        item_rot, tr = t[:9].reshape(3, 3), t[9:]
        m = part_matrix[oid]
        verts = mesh(comp_path[oid])
        local = (m[:3, :3] @ verts.T).T + m[:3, 3]
        world = local @ item_rot + tr
        up = item_rot.T @ m[:3, :3] @ np.array([0.0, 0.0, 1.0])  # where the part's +Z ends up
        tilted = not np.allclose(up, [0, 0, 1], atol=1e-6)
        assert oid in plate_of, f"object {oid} is not on any plate"
        out.append((source_of[oid], world, tilted, plate_of[oid]))
    out.sort(key=lambda r: r[0])
    assert [r[0] for r in out] == list(range(len(out))), "source_object_id is not a permutation of the input"
    return [r[1:] for r in out], len(plates)


def plate_window(k, n_plates, bed):
    cols = math.ceil(math.sqrt(n_plates))
    ox = (k % cols) * PLATE_GAP * bed[0]
    oy = -(k // cols) * PLATE_GAP * bed[1]
    return ox, oy


def check_on_bed(content, expected, bed):
    """expected: flat list of Part, one per instance in build order."""
    items, n_plates = placements(content)
    assert len(items) == len(expected)
    for idx, ((world, tilted, plate), part) in enumerate(zip(items, expected)):
        label = f"{part.name}#{idx} (plate {plate + 1}/{n_plates})"
        assert np.allclose(_shape_signature(world), _shape_signature(part.verts), atol=1e-3), \
            f"{label}: geometry changed"
        ox, oy = plate_window(plate, n_plates, bed)
        lo, hi = world.min(axis=0), world.max(axis=0)
        assert lo[0] >= ox - TOL and hi[0] <= ox + bed[0] + TOL, f"{label}: off bed in X {lo[0]:.2f}..{hi[0]:.2f}"
        assert lo[1] >= oy - TOL and hi[1] <= oy + bed[1] + TOL, f"{label}: off bed in Y {lo[1]:.2f}..{hi[1]:.2f}"
        assert hi[2] <= bed[2] + TOL, f"{label}: taller than bed_z"
        assert not tilted, f"{label}: tilted about X/Y"
        assert lo[2] == pytest.approx(part.verts[:, 2].min(), abs=TOL), \
            f"{label}: min Z moved from {part.verts[:, 2].min()} to {lo[2]:.3f}"


def expand(parts, qtys):
    return [p for p, q in zip(parts, qtys) for _ in range(q)]


A = Part("a.stl", 40, 40, 0, 40)
B = Part("b.stl", 20, 20, 0, 20)
TALL = Part("tall.stl", 10, 10, 0, 80)


@pytest.mark.parametrize("parts,qtys,bed", [
    ([A, B], [3, 1], (100, 100, 100)),                  # issue acceptance case
    ([A], [3], (60, 60, 100)),                          # one per plate, 3 plates
    ([A], [8], (60, 60, 100)),                          # 3x3 plate grid
    ([B], [12], (100, 100, 100)),                       # many per plate
    ([A, B], [4, 5], (150, 80, 100)),                   # non-square bed, mixed sizes
    ([TALL, B], [3, 2], (100, 100, 100)),               # Orca lays the tall part down
], ids=["acceptance", "one_per_plate", "plate_grid", "many_per_plate", "rect_bed", "reoriented"])
def test_all_instances_on_their_plate(parts, qtys, bed):
    check_on_bed(pack(parts, qtys, bed), expand(parts, qtys), bed)


def test_checker_detects_off_bed():
    """The check must fail when the bed it is given is smaller than the real one."""
    content = pack([A], [1], (100, 100, 100))
    with pytest.raises(AssertionError, match="off bed"):
        check_on_bed(content, [A], (45, 45, 100))


FLOAT = Part("floating.stl", 20, 20, 5, 25)
BELOW = Part("below_plate.stl", 20, 20, -10, 10)
HIGH = Part("high.stl", 15, 25, 12.5, 40)


@pytest.mark.parametrize("parts,qtys,bed", [
    ([FLOAT], [2], (100, 100, 100)),
    ([BELOW], [2], (100, 100, 100)),
    ([FLOAT, BELOW, HIGH, A], [2, 2, 1, 1], (100, 100, 100)),  # distinct non-zero min Z's, plus one on the plate
    ([FLOAT, BELOW, HIGH], [3, 3, 3], (60, 60, 100)),          # ...spread over several plates
], ids=["floating", "below_plate", "mixed_min_z", "mixed_min_z_multi_plate"])
def test_min_z_preserved_for_offset_models(parts, qtys, bed):
    check_on_bed(pack(parts, qtys, bed), expand(parts, qtys), bed)


def test_tall_part_not_tilted():
    content = pack([TALL], [3], (100, 100, 100))
    for world, tilted, _ in placements(content)[0]:
        assert not tilted
        assert np.ptp(world[:, 2]) == pytest.approx(80, abs=TOL)


def _arrange(content: bytes, **data) -> bytes:
    # Orca on Linux segfaults (exit 139) loading a 3MF carrying its own version metadata
    strip = re.compile(rb'[ \t]*<metadata\s+name="(?:Application|OrcaSlicer)"\s*>[^<]*</metadata>\r?\n?')
    src, buf = zipfile.ZipFile(io.BytesIO(content)), io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            raw = src.read(info.filename)
            dst.writestr(info.filename, strip.sub(b"", raw) if info.filename.endswith(".model") else raw)
    content = buf.getvalue()
    r = httpx.post(f"{BASE}/api/v1/arrange", files={"file": ("in.3mf", content, "application/octet-stream")},
                   data=data, timeout=300)
    assert r.status_code == 200, r.text
    return r.content


def _reverse_build_order(content: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(content))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "3D/3dmodel.model":
                text = data.decode()
                items = re.findall(r"<item\b[^>]*/>", text)
                start = text.index(items[0])
                end = text.index(items[-1]) + len(items[-1])
                text = text[:start] + "\n".join(reversed(items)) + text[end:]
                data = text.encode()
            dst.writestr(info.filename, data)
    return out.getvalue()


def _shapes_with_min_z(content):
    items, _ = placements(content)
    return sorted((tuple(np.round(_shape_signature(w), 2)), round(float(w[:, 2].min()), 3)) for w, _, _ in items)


@pytest.mark.parametrize("reverse", [False, True], ids=["build_order", "reversed_build_order"])
def test_arrange_preserves_min_z_per_object(reverse):
    parts, qtys, bed = [FLOAT, BELOW, HIGH, A], [2, 2, 1, 1], (100, 100, 100)
    packed = pack(parts, qtys, bed)
    src = _reverse_build_order(packed) if reverse else packed
    arranged = _arrange(src, arrange="true")
    before = _shapes_with_min_z(src)
    assert len({z for _, z in before}) >= 4          # several distinct min Z's, incl. non-zero
    assert _shapes_with_min_z(arranged) == before


def test_arrange_does_not_tilt_by_default():
    packed = pack([TALL], [2], (100, 100, 100))
    for world, tilted, _ in placements(_arrange(packed, arrange="true"))[0]:
        assert not tilted and np.ptp(world[:, 2]) == pytest.approx(80, abs=TOL)
