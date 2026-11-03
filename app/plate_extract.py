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
# Per-plate pauses / colour changes / custom layer G-code, keyed by <plate_info id="N"/>.
CUSTOM_GCODE = "Metadata/custom_gcode_per_layer.xml"
PLATE_GAP = 0.2

# Per-plate files Orca/Bambu write under Metadata/ (thumbnails, sliced output).
_PER_PLATE_FILE = re.compile(
    r"Metadata/(plate|plate_no_light|top|pick)_(\d+)(\.png|\.json|\.gcode|\.gcode\.md5)$")
_BUILD_RE = re.compile(r"(<build\b[^>]*>)(.*?)(</build>)", re.S)
_ITEM_RE = re.compile(r"<item\b[^>]*?/>", re.S)
_ATTR_RE = r'\b{}="([^"]*)"'


class PlateExtractError(ValueError):
    """The requested plate cannot be extracted (missing, empty, or unknown bed size)."""


class PlateFitError(PlateExtractError):
    """The extracted plate's printed footprint does not fit the target bed (BIZ-355)."""


def _attr(tag: str, name: str) -> Optional[str]:
    m = re.search(_ATTR_RE.format(re.escape(name)), tag)
    return m.group(1) if m else None


def _set_transform(tag: str, transform: str) -> str:
    if _attr(tag, "transform") is not None:
        return re.sub(r'\btransform="[^"]*"', f'transform="{transform}"', tag, count=1)
    return tag.replace("<item", f'<item transform="{transform}"', 1)


def bed_rect(project_settings: dict) -> Optional[tuple[float, float, float, float]]:
    """(x0, y0, x1, y1) bounding ``printable_area`` (list of "XxY" points), or None if absent/garbled."""
    area = project_settings.get("printable_area")
    if not isinstance(area, list) or not area:
        return None
    try:
        pts = [tuple(float(v) for v in str(p).split("x")) for p in area]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    except (ValueError, IndexError):
        return None
    rect = (min(xs), min(ys), max(xs), max(ys))
    return rect if rect[2] > rect[0] and rect[3] > rect[1] else None


def bed_size(project_settings: dict) -> Optional[tuple[float, float]]:
    """(W, D) of ``printable_area``, or None if absent/garbled."""
    rect = bed_rect(project_settings)
    return (rect[2] - rect[0], rect[3] - rect[1]) if rect else None


def plate_origin(index0: int, n_plates: int, w: float, d: float) -> tuple[float, float]:
    cols = max(1, math.ceil(math.sqrt(n_plates)))
    col, row = index0 % cols, index0 // cols
    return col * w * (1 + PLATE_GAP), -row * d * (1 + PLATE_GAP)


def _meta(el: ET.Element, key: str) -> Optional[ET.Element]:
    for m in el.findall("metadata"):
        if m.get("key") == key:
            return m
    return None


def _custom_gcode_for_plate(data: bytes, plate: int) -> tuple[bytes, int, list[int]]:
    """Keep only *plate*'s block of custom_gcode_per_layer.xml, renumbered to plate 1.

    Returns (xml, entries kept, other plate ids dropped). Without this, slicing plate N
    would silently pick up plate 1's pauses/colour changes and lose its own.
    """
    root = _subset._parse(data)
    kept, dropped = 0, []
    for pl in list(root.findall("plate")):
        info = pl.find("plate_info")
        pid = int(info.get("id")) if info is not None and info.get("id") else None
        if pid == plate:
            info.set("id", "1")
            kept = len(pl.findall("layer"))
        else:
            dropped.append(pid)
            root.remove(pl)
    return (b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8"),
            kept, dropped)


def extract_plate(src: str, plate: int, dst: str, fallback_settings: Optional[dict] = None) -> dict:
    """Write *dst*: *src* reduced to plate *plate*, renumbered to plate 1 at plate-1 coordinates.

    The plate grid is the one *src* was laid out on, so the stride comes from *src*'s own
    ``printable_area``; *fallback_settings* (the target machine) is only consulted when *src*
    has none. ``source_bed`` in the result is the bed rect used (None if neither had one).

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
        try:
            cfg = json.loads(z.read(PROJECT_SETTINGS)) if PROJECT_SETTINGS in names else {}
        except ValueError:
            cfg = {}
        rect = bed_rect(cfg if isinstance(cfg, dict) else {})
        if rect is None and fallback_settings:
            rect = bed_rect(fallback_settings)
        bed = (rect[2] - rect[0], rect[3] - rect[1]) if rect else None
        dx = dy = 0.0
        if index0:
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

        custom_gcode = None
        if CUSTOM_GCODE in names:
            custom_out, cg_kept, cg_dropped = _custom_gcode_for_plate(z.read(CUSTOM_GCODE), plate)
            custom_gcode = {"kept": cg_kept, "dropped_plates": cg_dropped}

        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                name = info.filename
                if name == MODEL:
                    out.writestr(info, model.encode("utf-8"))
                elif name == MODEL_SETTINGS:
                    out.writestr(info, settings_out)
                elif name == CUSTOM_GCODE:
                    out.writestr(info, custom_out)
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
        "source_bed": list(rect) if rect else None,
        "origin_shift": [round(dx, 3), round(dy, 3)],
        "kept_items": len(kept),
        "dropped_items": dropped,
        "missing_instances": missing,
        "objects": sorted(set(kept), key=lambda s: int(s) if s.isdigit() else 0),
        "positions": moved,
        "custom_gcode": custom_gcode,
    }


# ------------------------------------------------------------------ retarget (BIZ-355)

FIT_TOLERANCE_MM = 0.05


def _printable_height(project_settings: dict) -> Optional[float]:
    v = project_settings.get("printable_height")
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        h = float(v)
    except (TypeError, ValueError):
        return None
    return h if h > 0 else None


def item_boxes(z: zipfile.ZipFile) -> list[tuple[str, list[float], list[float]]]:
    """Per build item with geometry, in build order: (object_id, [x0, y0, z0], [x1, y1, z1]).

    Printed parts only (modifier / negative / blocker / enforcer parts are ignored).
    """
    from app import preview_3mf as _pv  # numpy; only needed here

    model = _subset._parse(z.read(MODEL))
    excluded: dict[str, set[int]] = {}
    if MODEL_SETTINGS in z.namelist():
        for o in _subset._parse(z.read(MODEL_SETTINGS)).findall("object"):
            excluded[o.get("id")] = {int(p.get("id")) for p in o.findall("part")
                                     if p.get("subtype") in _pv._NON_PRINTED_PARTS}
    res = model.find(_subset._q("resources"))
    res_by_id = {o.get("id"): o for o in res.findall(_subset._q("object"))} if res is not None else {}
    build = model.find(_subset._q("build"))
    src = _pv._Source(z)
    geos: dict[str, list] = {}
    out = []
    for it in (build.findall(_subset._q("item")) if build is not None else []):
        oid = it.get("objectid")
        if oid not in geos:
            obj = res_by_id.get(oid)
            geos[oid] = (_pv._object_geometry(src, obj, res_by_id, excluded.get(oid, set()))
                         if obj is not None else [])
        if not geos[oid]:
            continue
        m, t = _pv._mats(it.get("transform"))
        lo = [min(float((v @ m[:, a]).min()) for v, _ in geos[oid]) + float(t[a]) for a in range(3)]
        hi = [max(float((v @ m[:, a]).max()) for v, _ in geos[oid]) + float(t[a]) for a in range(3)]
        out.append((oid, lo, hi))
    return out


def plate_footprint(z: zipfile.ZipFile) -> Optional[tuple[float, float, float, float, float]]:
    """(x0, y0, x1, y1, height) of every build item's printed parts, or None without geometry.

    Height per item is its top minus its bottom, the bottom clamped at the bed (Orca drops
    floating parts onto it and cuts away what is sunk below it).
    """
    box: Optional[list[float]] = None
    height = 0.0
    for _, lo, hi in item_boxes(z):
        height = max(height, hi[2] - max(lo[2], 0.0))
        box = ([lo[0], lo[1], hi[0], hi[1]] if box is None else
               [min(box[0], lo[0]), min(box[1], lo[1]), max(box[2], hi[0]), max(box[3], hi[1])])
    return (*box, height) if box else None


def _mm(v: float) -> str:
    return f"{v:g}" if abs(v - round(v)) < 1e-6 else f"{v:.1f}"


def retarget_plate(src: str, dst: str, plate: int, source_bed: Optional[list],
                   target_settings: dict, centre_footprint: bool = False) -> dict:
    """Write *dst*: single-plate *src* (from extract_plate) moved onto the target bed, with
    *target_settings* as its project_settings.

    The plate is shifted from the source bed's centre to the target bed's centre, which
    keeps the authored layout (pairwise offsets) intact. If the footprint fits the target
    bed but that placement would overflow it, the footprint bbox is centred instead.
    Raises PlateFitError when the printed footprint (X, Y, or height) exceeds the target bed.
    *source_bed* is ``extract_plate``'s ``source_bed`` (None: treat as the target bed).
    *centre_footprint* always centres the footprint bbox on the target bed.
    """
    target = bed_rect(target_settings)
    if target is None:
        raise PlateExtractError("target machine has no usable printable_area")
    tx0, ty0, tx1, ty1 = target
    tw, td, th = tx1 - tx0, ty1 - ty0, _printable_height(target_settings)
    sx0, sy0, sx1, sy1 = source_bed or target
    dx, dy = (tx0 + tx1 - sx0 - sx1) / 2, (ty0 + ty1 - sy0 - sy1) / 2
    placement = "bed_centre"
    eps = FIT_TOLERANCE_MM
    with _subset.open_zip(src) as z:
        fp = plate_footprint(z)
        if fp:
            x0, y0, x1, y1, h = fp
            fw, fd = x1 - x0, y1 - y0
            if fw > tw + eps or fd > td + eps or (th is not None and h > th + eps):
                bed_txt = " × ".join(_mm(v) for v in (tw, td) + ((th,) if th is not None else ()))
                raise PlateFitError(
                    f"Plate {plate} footprint {fw:.1f} × {fd:.1f} × {h:.1f} mm does not fit "
                    f"the target bed {bed_txt} mm")
            if centre_footprint or not (x0 + dx >= tx0 - eps and x1 + dx <= tx1 + eps
                    and y0 + dy >= ty0 - eps and y1 + dy <= ty1 + eps):
                dx, dy = (tx0 + tx1 - x0 - x1) / 2, (ty0 + ty1 - y0 - y1) / 2
                placement = "footprint_centre"

        model = z.read(MODEL).decode("utf-8")
        bm = _BUILD_RE.search(model)
        if bm is None:
            raise PlateExtractError("3D/3dmodel.model has no <build> section")

        def _shift(m: re.Match) -> str:
            tag = m.group(0)
            tf = [float(v) for v in (_attr(tag, "transform") or "1 0 0 0 1 0 0 0 1 0 0 0").split()]
            tf[9] += dx
            tf[10] += dy
            return _set_transform(tag, " ".join(f"{v:.9g}" for v in tf))

        model = model[:bm.start(2)] + _ITEM_RE.sub(_shift, bm.group(2)) + model[bm.end(2):]
        payload = json.dumps(target_settings, ensure_ascii=False, indent=2).encode("utf-8")
        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                if info.filename == PROJECT_SETTINGS:
                    continue
                out.writestr(info, model.encode("utf-8") if info.filename == MODEL
                             else z.read(info.filename))
            out.writestr(PROJECT_SETTINGS, payload)

    return {
        "placement": placement,
        "shift": [round(dx, 3), round(dy, 3)],
        "footprint_mm": ([round(fp[2] - fp[0], 3), round(fp[3] - fp[1], 3), round(fp[4], 3)]
                         if fp else None),
        "target_bed_mm": [round(tw, 3), round(td, 3), th],
    }
