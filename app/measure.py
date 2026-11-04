"""``POST /api/v1/projects/measure``: sizes of objects, plates or a whole file (BIZ-363).

Sizes are axis-aligned bounding boxes of printed parts (modifier / negative / support
blocker and enforcer parts are ignored), in mm, after the build transform:

- object (``id`` / ``name``): its first instance as placed, so a rotated object reports its
  placed size;
- plate (``plate``): every instance on that plate as authored, i.e. the footprint a locked or
  together plate needs;
- whole file (no selection): every build item; a project with more than one plate needs a
  ``plate`` selector instead (its plates are laid out side by side, not stacked);
- STL: the mesh extents.
"""
from __future__ import annotations

import zipfile
from typing import Optional

from app import plate_extract as _pe
from app import subset_3mf as _subset
from app.pack_mixed import _numbered
from app.stl_to_3mf import parse_stl_triangles


class MeasureError(ValueError):
    """Unreadable input or a selector that names nothing measurable."""


Box = tuple[list[float], list[float]]


def _size(boxes: list[Box]) -> list[float]:
    lo = [min(b[0][a] for b in boxes) for a in range(3)]
    hi = [max(b[1][a] for b in boxes) for a in range(3)]
    return [round(hi[a] - lo[a], 3) for a in range(3)]


def measure_stl(path: str, selection: list[dict]) -> list[list[float]]:
    if selection:
        raise MeasureError("an STL is measured whole; send no selection")
    tris = parse_stl_triangles(path)
    if not tris:
        raise MeasureError("no triangles (empty or unparseable STL)")
    return [_size([([min(v[a] for t in tris for v in t) for a in range(3)],
                     [max(v[a] for t in tris for v in t) for a in range(3)])])]


def measure_3mf(path: str, selection: list[dict]) -> list[list[float]]:
    try:
        with _subset.open_zip(path) as z:
            items = _pe.item_boxes(z)
            ms = _subset._parse(z.read(_subset.MODEL_SETTINGS)) if _subset.MODEL_SETTINGS in z.namelist() else None
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise MeasureError(f"cannot read 3MF: {e}")
    # objects without geometry are skipped whole, so per-object numbering still lines up
    boxes: dict[tuple[str, int], Box] = dict(zip(_numbered(oid for oid, _, _ in items),
                                                 ((lo, hi) for _, lo, hi in items)))
    plates: dict[int, list[tuple[str, int]]] = {}
    names: dict[str, list[str]] = {}
    if ms is not None:
        for n, pl in enumerate(ms.findall("plate"), start=1):
            plates[int(_subset._meta(pl, "plater_id") or n)] = [
                (_subset._meta(mi, "object_id"), int(_subset._meta(mi, "instance_id") or 0))
                for mi in pl.findall("model_instance")]
        for o in ms.findall("object"):
            names.setdefault(_subset._meta(o, "name") or "", []).append(o.get("id"))

    if not selection:
        if len(plates) > 1:
            raise MeasureError(f"the project has {len(plates)} plates; select one with {{\"plate\": N}}")
        if not boxes:
            raise MeasureError("nothing printable in the file")
        return [_size(list(boxes.values()))]

    out = []
    for g, sel in enumerate(selection):
        keys = [k for k in ("id", "name", "plate") if k in sel]
        if len(keys) != 1:
            raise MeasureError(f"selection[{g}]: give exactly one of id, name, plate")
        if keys[0] == "plate":
            members = plates.get(_int(sel["plate"], g, "plate"))
            if members is None:
                raise MeasureError(f"selection[{g}]: no plate {sel['plate']}")
            got = [boxes[m] for m in members if m in boxes]
            if not got:
                raise MeasureError(f"selection[{g}]: plate {sel['plate']} has nothing printable")
            out.append(_size(got))
            continue
        if keys[0] == "name":
            ids = names.get(str(sel["name"]), [])
            if len(ids) != 1:
                raise MeasureError(f"selection[{g}]: name {sel['name']!r} matches {len(ids)} objects; use id")
            oid = ids[0]
        else:
            oid = str(_int(sel["id"], g, "id"))
        box = boxes.get((oid, 0))
        if box is None:
            raise MeasureError(f"selection[{g}]: object {oid} has no placed, printable instance")
        out.append(_size([box]))
    return out


def _int(v, g: int, key: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise MeasureError(f"selection[{g}]: {key} must be an integer")
    return v


def measure(path: str, stl: bool, selection: Optional[list[dict]]) -> list[list[float]]:
    return (measure_stl if stl else measure_3mf)(path, selection or [])
