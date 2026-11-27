"""Per-object labels and the neutral object manifest (BIZ-407).

Every slice must emit object labels so a consumer can address and draw each object:
EXCLUDE_OBJECT_* (Klipper) / ``M486`` (Marlin) in the G-code and per-object entries in the
``.gcode.3mf`` plate metadata. Labels are opaque, slicer-owned and unique within a job.
This module knows nothing about who asked for the slice.

Pinned OrcaSlicer 2.5.x (verified against the real binary):

* ``exclude_object = 1`` is what makes Orca write the object definitions: Klipper
  ``EXCLUDE_OBJECT_DEFINE NAME= CENTER= POLYGON=`` / ``EXCLUDE_OBJECT_START|END``, Marlin
  ``M486 A<name>`` / ``M486 S<n>``, and ``label_object_enabled`` in ``slice_info.config``.
* ``gcode_label_objects = 1`` adds the ``; printing object NAME id:I copy K`` comments.
  Alone it emits neither definitions nor polygons, so both keys are forced.

Orca's label is ``<object name>_id_<object index>_copy_<instance>``: unique within one plate
even for two copies of one model, because the object index is unique. Its ``plate_N.json``
``name`` is the raw object name though, so copies collide there and never equal the G-code
label. ``align_plate_names`` rewrites those names to the label (deterministically: name prefix,
then nearest footprint), and ``build_manifest`` refuses an output whose labels are not unique.
"""
from __future__ import annotations

import json
import re
import zipfile
from typing import Optional

VERSION = 1

# Deliberate override: applied to every slice whatever the preset / extra_config says.
FORCED_SETTINGS = {"exclude_object": "1", "gcode_label_objects": "1"}

SETTINGS = "Metadata/project_settings.config"


class LabelError(ValueError):
    """The slice output does not carry usable object labels."""


# ---------------------------------------------------------------- before slicing
def _rewrite(path: str, replace: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path) as z:
        entries = [(i, z.read(i.filename)) for i in z.infolist()]
    present = {i.filename for i, _ in entries}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as out:
        for info, data in entries:
            out.writestr(info, replace.get(info.filename, data))
        for name, data in replace.items():
            if name not in present:
                out.writestr(name, data)


def force_settings(path: str) -> None:
    """Force the label settings into the 3MF's project_settings.config at *path* (in place)."""
    with zipfile.ZipFile(path) as z:
        cfg = json.loads(z.read(SETTINGS)) if SETTINGS in z.namelist() else {}
    cfg.update(FORCED_SETTINGS)
    _rewrite(path, {SETTINGS: json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")})


# ---------------------------------------------------------------- after slicing
_NUM = r"(-?\d+(?:\.\d+)?)"
_XY = re.compile(rf"\b([XY]){_NUM}")


def parse_gcode(text: str) -> list[dict]:
    """[{label, polygon|None, extent|None}] in definition order, from EXCLUDE_OBJECT / M486.

    ``polygon`` is Orca's own footprint (Klipper only); ``extent`` = [x0, y0, x1, y1] is the
    bounding box of the object's extrusion moves.
    """
    objs: dict[str, dict] = {}
    order: list[str] = []
    m486: list[str] = []
    cur: Optional[str] = None
    x = y = None
    for line in text.splitlines():
        if not line or line[0] == ";":
            continue
        if line.startswith("EXCLUDE_OBJECT_DEFINE"):
            name = re.search(r"NAME=(\S+)", line)
            poly = re.search(r"POLYGON=(\[.*\])", line)
            if name:
                n = name.group(1)
                objs.setdefault(n, {"label": n, "polygon": None, "extent": None})
                order.append(n)
                if poly:
                    objs[n]["polygon"] = [[float(a), float(b)] for a, b in json.loads(poly.group(1))]
            continue
        if line.startswith("EXCLUDE_OBJECT_START"):
            m = re.search(r"NAME=(\S+)", line)
            cur = m.group(1) if m else None
            continue
        if line.startswith("EXCLUDE_OBJECT_END"):
            cur = None
            continue
        if line.startswith("M486"):
            a = re.match(r"M486 A(\S.*)", line)
            if a:
                n = a.group(1).strip()
                m486.append(n)
                objs.setdefault(n, {"label": n, "polygon": None, "extent": None})
                order.append(n)
                continue
            s = re.match(r"M486 S(-?\d+)", line)
            if s:
                i = int(s.group(1))
                cur = m486[i] if 0 <= i < len(m486) else None
            continue
        if line[0] == "G" and line[:3] in ("G1 ", "G2 ", "G3 "):
            for ax, v in _XY.findall(line):
                if ax == "X":
                    x = float(v)
                else:
                    y = float(v)
            if cur in objs and x is not None and y is not None and re.search(r"\bE[0-9.]", line):
                e = objs[cur]["extent"]
                objs[cur]["extent"] = ([x, y, x, y] if e is None else
                                       [min(e[0], x), min(e[1], y), max(e[2], x), max(e[3], y)])
    return [objs[n] for n in order]


def _bed(settings: dict) -> dict:
    pts = []
    for p in settings.get("printable_area") or []:
        try:
            a, b = str(p).lower().split("x")
            pts.append((float(a), float(b)))
        except ValueError:
            continue
    if not pts:
        raise LabelError("project has no printable_area")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {"origin": [min(xs), min(ys)], "size": [max(xs) - min(xs), max(ys) - min(ys)]}


def _rect(b: list[float]) -> list[list[float]]:
    return [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]]]


def _bounds(poly: list[list[float]]) -> list[float]:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def build_manifest(plate: int, gcode: str, settings: dict) -> dict:
    """Neutral object manifest for one sliced plate (labels from the G-code itself)."""
    objects = parse_gcode(gcode)
    if not objects:
        raise LabelError("slice output carries no object labels (EXCLUDE_OBJECT_DEFINE / M486 A)")
    labels = [o["label"] for o in objects]
    if len(set(labels)) != len(labels):
        raise LabelError(f"object labels are not unique: {sorted(labels)}")
    out = []
    for o in objects:
        poly = o["polygon"] or (_rect(o["extent"]) if o["extent"] else None)
        if poly is None:
            raise LabelError(f"object {o['label']} has no footprint")
        out.append({"label": o["label"], "polygon": poly, "bbox": _bounds(poly)})
    return {"version": VERSION, "plates": [{"plate": plate, "bed": _bed(settings), "objects": out}]}


def align_plate_names(threemf: str, manifest: dict) -> None:
    """Rewrite ``Metadata/plate_N.json`` object names to the G-code labels (in place).

    Orca stores the raw object name there; matched by name prefix, then nearest bbox centre
    among instances of one object.
    """
    objs = manifest["plates"][0]["objects"]
    with zipfile.ZipFile(threemf) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"Metadata/plate_\d+\.json", n)]
        if not names:
            return
        pj = json.loads(z.read(names[0]))
    left = list(objs)
    for e in pj.get("bbox_objects", []):
        raw = re.sub(r"[^A-Za-z0-9_]", "_", e.get("name") or "")
        cands = [o for o in left if re.fullmatch(re.escape(raw) + r"_id_\d+_copy_\d+", o["label"])]
        if not cands:
            raise LabelError(f"plate metadata object {e.get('name')!r} matches no G-code label")
        cx, cy = (e["bbox"][0] + e["bbox"][2]) / 2, (e["bbox"][1] + e["bbox"][3]) / 2
        best = min(cands, key=lambda o: abs((o["bbox"][0] + o["bbox"][2]) / 2 - cx)
                   + abs((o["bbox"][1] + o["bbox"][3]) / 2 - cy))
        left.remove(best)
        e["name"] = best["label"]
    if left:
        raise LabelError(f"G-code objects missing from plate metadata: {[o['label'] for o in left]}")
    _rewrite(threemf, {names[0]: json.dumps(pj).encode("utf-8")})


def read_threemf_settings(threemf: str) -> dict:
    with zipfile.ZipFile(threemf) as z:
        return json.loads(z.read(SETTINGS))


def read_threemf_gcode(threemf: str, plate: int) -> str:
    with zipfile.ZipFile(threemf) as z:
        return z.read(f"Metadata/plate_{plate}.gcode").decode("utf-8", errors="replace")


def manifest_for_output(output: str, plate: int, sliced_plate: int, settings: dict) -> dict:
    """Manifest for a slice artifact (.gcode or .gcode.3mf); aligns the 3MF's plate names.

    *plate* is the plate the caller asked for; *sliced_plate* the one Orca sliced in *output*.
    """
    if output.lower().endswith(".3mf"):
        manifest = build_manifest(plate, read_threemf_gcode(output, sliced_plate),
                                  read_threemf_settings(output))
        align_plate_names(output, manifest)
        return manifest
    with open(output, encoding="utf-8", errors="replace") as fh:
        return build_manifest(plate, fh.read(), settings)
