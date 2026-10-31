"""Pull one plate of a multi-plate 3MF out into a standalone single-plate project (BIZ-349).

OrcaSlicer's CLI is unreliable when asked to slice plate N of a multi-plate project:
``--arrange 1`` re-packs every object across plates (it can collapse three plates into
one, after which ``--slice 2`` "succeeds" with nothing sliced, or move parts off the
requested plate), and ``--arrange 0`` judges plate bounds with a plate stride that does
not always match the one the project was laid out with (-50 "no object fully inside").

Slicing a project that only contains the requested plate, already moved to plate-1
coordinates, with ``--slice 1 --arrange 0`` avoids both: nothing can be re-packed and the
plate-1 bounds are the bed itself.

Orca lays plates out on a grid: ``cols = ceil(sqrt(n_plates))``; plate *i* (0-based) has
its origin at ``(col * W * 1.2, -row * D * 1.2)`` where ``col = i % cols``, ``row = i // cols``
and W x D is the bed size from ``printable_area`` (LOGICAL_PART_PLATE_GAP = 1/5).
"""
from __future__ import annotations

import json
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional

from app import subset_3mf as _subset

MODEL = "3D/3dmodel.model"
MODEL_SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
PLATE_GAP = 0.2

# Per-plate files Orca/Bambu write under Metadata/ (thumbnails, sliced output).
_PER_PLATE_FILE = re.compile(
    r"Metadata/(plate|plate_no_light|top|pick)_(\d+)(\.png|\.json|\.gcode|\.gcode\.md5)$")
_BUILD_RE = re.compile(r"(<build\b[^>]*>)(.*?)(</build>)", re.S)
_ITEM_RE = re.compile(r"<item\b[^>]*?/>", re.S)
_ATTR_RE = r'\b{}="([^"]*)"'


class PlateExtractError(ValueError):
    """The requested plate cannot be extracted (missing, empty, or unknown bed size)."""


def _attr(tag: str, name: str) -> Optional[str]:
    m = re.search(_ATTR_RE.format(re.escape(name)), tag)
    return m.group(1) if m else None


def _set_transform(tag: str, transform: str) -> str:
    if _attr(tag, "transform") is not None:
        return re.sub(r'\btransform="[^"]*"', f'transform="{transform}"', tag, count=1)
    return tag.replace("<item", f'<item transform="{transform}"', 1)


def bed_size(project_settings: dict) -> Optional[tuple[float, float]]:
    """(W, D) of ``printable_area`` (list of "XxY" points), or None if absent/garbled."""
    area = project_settings.get("printable_area")
    if not isinstance(area, list) or not area:
        return None
    try:
        pts = [tuple(float(v) for v in str(p).split("x")) for p in area]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    except (ValueError, IndexError):
        return None
    w, d = max(xs) - min(xs), max(ys) - min(ys)
    return (w, d) if w > 0 and d > 0 else None


def plate_origin(index0: int, n_plates: int, w: float, d: float) -> tuple[float, float]:
    cols = max(1, math.ceil(math.sqrt(n_plates)))
    col, row = index0 % cols, index0 // cols
    return col * w * (1 + PLATE_GAP), -row * d * (1 + PLATE_GAP)


def _meta(el: ET.Element, key: str) -> Optional[ET.Element]:
    for m in el.findall("metadata"):
        if m.get("key") == key:
            return m
    return None


def extract_plate(src: str, plate: int, dst: str) -> dict:
    """Write *dst*: *src* reduced to plate *plate*, renumbered to plate 1 at plate-1 coordinates.

    Returns a summary for logging. ``has_plate_data`` is False (and *dst* is a plain copy)
    when *src* declares no plates at all (e.g. an STL-derived 3MF); that is only valid for
    plate 1. Raises PlateExtractError when the plate is missing or empty, or the bed size
    needed for the origin math is unknown.
    """
    if plate < 1:
        raise PlateExtractError("plate must be >= 1")
    with _subset.open_zip(src) as z:
        names = z.namelist()
        settings_xml = z.read(MODEL_SETTINGS) if MODEL_SETTINGS in names else None
        root = _subset._parse(settings_xml) if settings_xml else None
        plates = root.findall("plate") if root is not None else []

        if not plates:
            if plate != 1:
                raise PlateExtractError(f"project declares no plates; cannot slice plate {plate}")
            with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as out:
                for info in z.infolist():
                    out.writestr(info, z.read(info.filename))
            return {"has_plate_data": False, "source_plates": 0, "plate": 1}

        by_id = {}
        for n, pl in enumerate(plates, start=1):
            pid_el = _meta(pl, "plater_id")
            by_id[int(pid_el.get("value")) if pid_el is not None else n] = pl
        if plate not in by_id:
            raise PlateExtractError(
                f"plate {plate} not found (project has plates {sorted(by_id)})")
        target = by_id[plate]
        wanted: set[tuple[str, int]] = set()
        for mi in target.findall("model_instance"):
            oid, iid = _meta(mi, "object_id"), _meta(mi, "instance_id")
            if oid is not None:
                wanted.add((oid.get("value"), int(iid.get("value")) if iid is not None else 0))
        if not wanted:
            raise PlateExtractError(f"plate {plate} has no objects")

        n_plates = len(by_id)
        index0 = sorted(by_id).index(plate)
        dx = dy = 0.0
        bed = None
        if index0:
            try:
                cfg = json.loads(z.read(PROJECT_SETTINGS)) if PROJECT_SETTINGS in names else {}
            except ValueError:
                cfg = {}
            bed = bed_size(cfg)
            if bed is None:
                raise PlateExtractError(
                    f"cannot locate plate {plate}: project_settings has no usable printable_area")
            dx, dy = plate_origin(index0, n_plates, *bed)

        # --- 3D/3dmodel.model: keep this plate's build items, shifted onto plate 1 ---
        model = z.read(MODEL).decode("utf-8")
        bm = _BUILD_RE.search(model)
        if bm is None:
            raise PlateExtractError("3D/3dmodel.model has no <build> section")
        seen: dict[str, int] = {}
        kept, dropped, moved = [], 0, []

        def _keep(m: re.Match) -> str:
            nonlocal dropped
            tag = m.group(0)
            oid = _attr(tag, "objectid")
            idx = seen.get(oid, 0)
            seen[oid] = idx + 1
            if (oid, idx) not in wanted:
                dropped += 1
                return ""
            tf = [float(v) for v in (_attr(tag, "transform") or "1 0 0 0 1 0 0 0 1 0 0 0").split()]
            tf[9] -= dx
            tf[10] -= dy
            moved.append({"object_id": oid, "instance": idx, "x": round(tf[9], 3), "y": round(tf[10], 3)})
            kept.append(oid)
            return _set_transform(tag, " ".join(f"{v:.9g}" for v in tf))

        build_body = _ITEM_RE.sub(_keep, bm.group(2))
        missing = len(wanted) - len(kept)
        if not kept:
            raise PlateExtractError(f"plate {plate}: none of its instances have build items")
        model = model[:bm.start(2)] + build_body + model[bm.end(2):]

        # --- model_settings.config: only the target plate, as plate 1 ---
        for pl in plates:
            if pl is not target:
                root.remove(pl)
        pid_el = _meta(target, "plater_id")
        if pid_el is not None:
            pid_el.set("value", "1")
        for m in target.findall("metadata"):
            v = m.get("value") or ""
            if v.startswith("Metadata/"):
                m.set("value", re.sub(rf"_{plate}(\.\w+)$", r"_1\1", v))
        settings_out = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")

        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                name = info.filename
                if name == MODEL:
                    out.writestr(info, model.encode("utf-8"))
                elif name == MODEL_SETTINGS:
                    out.writestr(info, settings_out)
                else:
                    pm = _PER_PLATE_FILE.match(name)
                    if pm:
                        if int(pm.group(2)) != plate:
                            continue  # another plate's thumbnail / stale sliced output
                        info = zipfile.ZipInfo(f"Metadata/{pm.group(1)}_1{pm.group(3)}", info.date_time)
                        info.compress_type = zipfile.ZIP_DEFLATED
                    out.writestr(info, z.read(name))

    return {
        "has_plate_data": True,
        "plate": plate,
        "source_plates": n_plates,
        "plate_index0": index0,
        "bed_mm": list(bed) if bed else None,
        "origin_shift": [round(dx, 3), round(dy, 3)],
        "kept_items": len(kept),
        "dropped_items": dropped,
        "missing_instances": missing,
        "objects": sorted(set(kept), key=lambda s: int(s) if s.isdigit() else 0),
        "positions": moved,
    }
