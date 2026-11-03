"""``POST /api/v1/pack/next``: the fullest single plate a machine can print (BIZ-361).

Given the parts still to print (STLs, 3MF objects and 3MF plate bundles x qty, from several
files) the remaining-parts project is built like ``/projects/merge``, retargeted to the
machine, and OrcaSlicer arranges it. Plate 1 of that arrangement is the answer, after two
corrections, each followed by a fresh arrange of just the kept instances:

- ``together`` entries are all-or-nothing per copy: a copy Orca split across plates is
  dropped from the plate entirely (Orca's layout is used for the copy, not the authored one);
- ``max_parts`` trims whole units, last placed first, never splitting a ``together`` copy.

``solo`` entries are rejected: callers send those as their own jobs.

Every instance carries an origin ``{"item", "source", "part", "unit"}``; ``unit`` groups
the instances that must stay together (one per instance for ``split``).
"""
from __future__ import annotations

import json
import os
import zipfile
from typing import Optional

from app import plate_extract as _pe
from app import subset_3mf as _subset
from app.stl_to_3mf import _CONTENT_TYPES, _RELS, _REL_TYPE_3D, _NS_REL, _object_model_xml, \
    parse_stl_triangles

RULES = ("split", "together", "solo")
MAX_ROUNDS = 4
STL_OBJECT_ID = 2

Instance = tuple[str, int]          # (object id, instance index)
Origins = dict[str, list[dict]]     # {object id: [origin per instance]}


class NothingFits(Exception):
    """Nothing in the selection fits this machine's bed (or survives together / max_parts)."""

    def __init__(self, message: str, details: Optional[list[dict]] = None):
        super().__init__(message)
        self.details = details or []


# ------------------------------------------------------------------ inputs

def stl_project(stl_path: str, dst: str, name: str, project_settings: dict) -> None:
    """A one-object project for an STL, shaped like an Orca 3MF so the subset/merge path takes it."""
    tris = parse_stl_triangles(stl_path)
    if not tris:
        raise _subset.SubsetError(f"{name}: no triangles (empty or unparseable STL)")
    mesh_path = "3D/Objects/stl_mesh.model"
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
        'requiredextensions="p"><resources>'
        f'<object id="{STL_OBJECT_ID}" type="model"><components>'
        f'<component p:path="/{mesh_path}" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        '</components></object></resources><build>'
        f'<item objectid="{STL_OBJECT_ID}" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>'
        '</build></model>')
    label = os.path.splitext(name)[0].replace('"', "'").replace("<", "(").replace(">", ")").replace("&", "+")
    settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>'
        f'<object id="{STL_OBJECT_ID}"><metadata key="name" value="{label}"/>'
        '<metadata key="extruder" value="1"/>'
        f'<part id="1" subtype="normal_part"><metadata key="name" value="{label}"/></part></object>'
        '<plate><metadata key="plater_id" value="1"/>'
        f'<model_instance><metadata key="object_id" value="{STL_OBJECT_ID}"/>'
        '<metadata key="instance_id" value="0"/></model_instance></plate></config>')
    rels = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="{_NS_REL}">'
            f'<Relationship Target="/{mesh_path}" Id="rel-1" Type="{_REL_TYPE_3D}"/></Relationships>')
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr(_subset.MODEL, model)
        z.writestr("3D/_rels/3dmodel.model.rels", rels)
        z.writestr(mesh_path, _object_model_xml(tris, 1))
        z.writestr(_subset.MODEL_SETTINGS, settings)
        z.writestr(_subset.PROJECT_SETTINGS, json.dumps(project_settings, ensure_ascii=False))


def entry_origins(src: str, entries: list[tuple[int, dict]], source: str,
                  stl: bool, limit: int) -> tuple[dict[int, int], dict[int, list[dict]]]:
    """({object id: qty}, {object id: [origin per instance]}) for one source file.

    *entries*: (global selection index, selector) pairs. A ``together`` copy of a plate
    bundle is one unit; every other instance is its own unit. Raises SubsetError before
    allocating anything once the file's instances would exceed *limit*.
    """
    objects, plates = _subset.list_objects(src), _subset.plate_members(src)
    want: dict[int, int] = {}
    origins: dict[int, list[dict]] = {}
    total = 0
    for g, sel in entries:
        rule = sel.get("rule", "split")
        selector = {k: v for k, v in sel.items() if k != "rule"}
        if stl:
            selector = {"id": STL_OBJECT_ID, "qty": selector.get("qty", 1)}
        try:
            got = _subset.resolve_selection(objects, [selector], plates)
        except _subset.SubsetError as e:
            raise _subset.SubsetError(f"selection[{g}] ({source}): {e}")
        total += sum(got.values())
        if total > limit:
            raise _subset.SubsetError(f"selection[{g}] ({source}): more than {limit} instances in total")
        for oid, n in got.items():
            per_copy = plates[int(selector["plate"])][oid] if "plate" in selector else 1
            for j in range(n):
                unit = (g, j // per_copy) if rule == "together" else (g, oid, j)
                origins.setdefault(oid, []).append(
                    {"item": g, "source": source, "part": source if stl else str(oid), "unit": unit})
            want[oid] = want.get(oid, 0) + n
    return want, origins


def set_bed(path: str, printer_cfg: dict) -> dict:
    """Force the machine's bed keys into *path*'s project_settings; returns the settings."""
    with zipfile.ZipFile(path) as z:
        entries = [(i, z.read(i.filename)) for i in z.infolist()]
    cfg = {}
    for info, data in entries:
        if info.filename == _subset.PROJECT_SETTINGS:
            cfg = json.loads(data)
    for k in ("printable_area", "printable_height", "bed_exclude_area"):
        if k in printer_cfg:
            cfg[k] = printer_cfg[k]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in entries:
            if info.filename != _subset.PROJECT_SETTINGS:
                out.writestr(info, data)
        out.writestr(_subset.PROJECT_SETTINGS, json.dumps(cfg, ensure_ascii=False, indent=4))
    return cfg


# ------------------------------------------------------------------ instance bookkeeping

def all_instances(origins: Origins) -> list[Instance]:
    return [(oid, k) for oid, lst in origins.items() for k in range(len(lst))]


def units(origins: Origins) -> dict[tuple, set[Instance]]:
    out: dict[tuple, set[Instance]] = {}
    for oid, lst in origins.items():
        for k, o in enumerate(lst):
            out.setdefault(o["unit"], set()).add((oid, k))
    return out


def subset_instances(src: str, dst: str, origins: Origins, keep: set[Instance]) -> Origins:
    """Write *dst* with only the *keep* instances of *src*; returns their origins renumbered
    the way ``build_subset`` numbers them (k-th kept instance of an object -> instance k)."""
    want: dict[int, int] = {}
    new: Origins = {}
    for oid, lst in origins.items():
        kept = [lst[k] for k in range(len(lst)) if (oid, k) in keep]
        if kept:
            want[int(oid)] = len(kept)
            new[oid] = kept
    _subset.build_subset(src, dst, want)
    return new


def oversize(path: str, bed: tuple[float, float, Optional[float]]) -> dict[str, list[float]]:
    """{object id: [w, d, h]} of objects that cannot fit the bed in their current orientation."""
    w, d, h = bed
    eps = _pe.FIT_TOLERANCE_MM
    out: dict[str, list[float]] = {}
    with _subset.open_zip(path) as z:
        for oid, lo, hi in _pe.item_boxes(z):
            if oid in out:
                continue
            dims = [hi[0] - lo[0], hi[1] - lo[1], hi[2] - max(lo[2], 0.0)]
            if dims[0] > w + eps or dims[1] > d + eps or (h is not None and dims[2] > h + eps):
                out[oid] = [round(v, 1) for v in dims]
    return out


def choose(plate1: list[Instance], origins: Origins, max_parts: Optional[int]) -> list[Instance]:
    """Instances of Orca's plate 1 to keep: whole units only, in plate order, at most max_parts."""
    members = units(origins)
    on_plate = set(plate1)
    keep: list[Instance] = []
    done: set[tuple] = set()
    for inst in plate1:
        u = origins[inst[0]][inst[1]]["unit"]
        if u in done:
            continue
        done.add(u)
        group = members[u]
        if not group <= on_plate:
            continue                                  # a together copy Orca split: all or nothing
        if max_parts is not None and len(keep) + len(group) > max_parts:
            continue
        keep += [x for x in plate1 if x in group]
    return keep


def consumed(origins: Origins, n_items: int) -> list[int]:
    out = [0] * n_items
    for lst in origins.values():
        for o in lst:
            out[o["item"]] += 1
    return out
