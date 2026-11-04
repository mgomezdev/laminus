"""``POST /api/v1/pack/mixed``: a mixed selection on the fewest plates of one bed (BIZ-356).

The selection (STLs, 3MF objects and 3MF plate bundles x qty) is built into one project like
``/pack/next`` and arranged by OrcaSlicer across as many plates as it needs. Rules:

- ``split``: Orca's layout is taken as-is;
- ``together``: a copy Orca split across plates is taken off those plates and arranged again
  with the other split copies; a copy that still splits when arranged on its own is larger
  than one plate (``part_too_large``);
- ``solo``: never arranged; each instance gets a plate of its own, footprint centred.

Plates from the different arranges are combined here: every arranged plate is brought back
to plate-1 coordinates and the final project is laid out on Orca's plate grid again.
"""
from __future__ import annotations

from app import plate_extract as _pe
from app import subset_3mf as _subset
from app.pack_next import Instance, Origins

Placed = list[tuple[Instance, str]]   # one plate: (instance, transform in plate-1 coordinates)
MAX_COPY_ARRANGES = 8                 # together entries re-arranged one copy at a time, per request


def relabel(plate: Placed, rep: set[Instance], copy: set[Instance]) -> Placed:
    """*plate* (a layout of copy *rep*) for another copy of the same entry: the n-th instance
    of each object in *rep* becomes the n-th instance of that object in *copy*."""
    def ranked(insts):
        return {i: (i[0], n) for oid in {o for o, _ in insts}
                for n, i in enumerate(sorted(x for x in insts if x[0] == oid))}
    to = {v: k for k, v in ranked(copy).items()}
    rank = ranked(rep)
    return [(to[rank[i]], tf) for i, tf in plate]


def shift(tf: str, dx: float, dy: float) -> str:
    v = [float(x) for x in (tf or "1 0 0 0 1 0 0 0 1 0 0 0").split()]
    v[9] += dx
    v[10] += dy
    return " ".join(f"{x:.9g}" for x in v)


def renumber(keep: set[Instance]) -> dict[Instance, Instance]:
    """{instance in subset_instances(keep) output: instance it came from}."""
    by_oid: dict[str, list[int]] = {}
    for oid, k in keep:
        by_oid.setdefault(oid, []).append(k)
    return {(oid, j): (oid, k) for oid, ks in by_oid.items() for j, k in enumerate(sorted(ks))}


def to_local(transforms: dict[Instance, str], plates: list[list[Instance]],
             w: float, d: float) -> list[Placed]:
    """An arranged layout's plates, each moved back to plate-1 coordinates."""
    out = []
    for j, plate in enumerate(plates):
        ox, oy = _pe.plate_origin(j, len(plates), w, d)
        out.append([(i, shift(transforms[i], -ox, -oy)) for i in plate])
    return out


def split_units(plates: list[Placed], origins: Origins) -> set[tuple]:
    """Units (together copies) whose instances landed on more than one plate."""
    plate_of: dict[tuple, int] = {}
    split = set()
    for j, plate in enumerate(plates):
        for (oid, k), _ in plate:
            u = origins[oid][k]["unit"]
            if plate_of.setdefault(u, j) != j:
                split.add(u)
    return split


def _numbered(oids) -> list[Instance]:
    """Build-order object ids -> (object id, instance index)."""
    seen: dict[str, int] = {}
    out = []
    for oid in oids:
        out.append((oid, seen.get(oid, 0)))
        seen[oid] = out[-1][1] + 1
    return out


def solo_plates(path: str, solo: list[Instance], bed: tuple[float, float, float, float]) -> list[Placed]:
    """One plate per *solo* instance of *path*, its footprint centred on *bed* (x0, y0, x1, y1)."""
    with _subset.open_zip(path) as z:
        model = _subset._parse(z.read(_subset.MODEL))
        boxes = _pe.item_boxes(z)
    items = model.find(_subset._q("build")).findall(_subset._q("item"))
    tfs = dict(zip(_numbered(it.get("objectid") for it in items), (it.get("transform", "") for it in items)))
    # objects without geometry are skipped whole, so numbering per object still lines up
    box = dict(zip(_numbered(oid for oid, _, _ in boxes), ((lo, hi) for _, lo, hi in boxes)))
    cx, cy = (bed[0] + bed[2]) / 2, (bed[1] + bed[3]) / 2
    out = []
    for inst in solo:
        lo, hi = box.get(inst, ([cx, cy], [cx, cy]))
        out.append([(inst, shift(tfs[inst], cx - (lo[0] + hi[0]) / 2, cy - (lo[1] + hi[1]) / 2))])
    return out


def compose(plates: list[Placed], w: float, d: float) -> tuple[dict[Instance, str], list[list[Instance]]]:
    """(transforms, plate lists) for ``apply_layout``: *plates* laid out on Orca's plate grid."""
    tfs: dict[Instance, str] = {}
    out = []
    for j, plate in enumerate(plates):
        ox, oy = _pe.plate_origin(j, len(plates), w, d)
        for inst, tf in plate:
            tfs[inst] = shift(tf, ox, oy)
        out.append([inst for inst, _ in plate])
    return tfs, out
