"""Extract a subset of objects (with quantities) from an OrcaSlicer/Bambu 3MF.

Pure zip + XML surgery, no slicer involved. Mesh files are copied byte-for-byte, so
everything stored on triangles (paint_color / paint_supports / paint_seam / fuzzy skin)
and everything in Metadata/model_settings.config (per-object + per-part overrides,
modifier parts, extruder assignment) survives untouched. Quantities become extra build
*instances* of the one object, which is how Orca itself represents copies.

Plate arrangement is a separate step (OrcaSlicer ``--arrange``); this module only emits
a single-plate "everything piled on plate 1" project for the arranger to spread out.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
ET.register_namespace("", NS_CORE)
ET.register_namespace("p", NS_PROD)

MODEL = "3D/3dmodel.model"
MODEL_RELS = "3D/_rels/3dmodel.model.rels"
MODEL_SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"

# Per-plate / per-render artifacts that go stale when the plate layout changes.
_STALE = re.compile(
    r"^Metadata/(plate_\d+(_small)?|plate_no_light_\d+|top_\d+|pick_\d+)\.png$"
    r"|^Metadata/(custom_gcode_per_layer|slice_info)\.xml$"
)
# Per-object side files keyed by <object id="..."> that must be filtered.
_PER_OBJECT_FILES = ("Metadata/layer_config_ranges.xml", "Metadata/cut_information.xml")

# Machine-scoped project_settings keys swapped when retargeting to another printer.
PRINTER_KEYS = (
    "printable_area", "printable_height", "bed_exclude_area", "best_object_pos",
    "printer_model", "printer_variant", "printer_settings_id",
    "extruder_clearance_height_to_lid", "extruder_clearance_height_to_rod",
    "extruder_clearance_radius",
)


# Bambu Studio writes -1 ("auto") for options whose Orca minimum is 0; the Orca CLI
# rejects the whole project ("not in range") where the GUI would silently clamp.
_ORCA_MIN_CLAMP = {"raft_first_layer_expansion": 0, "tree_support_wall_count": 0}


class SubsetError(ValueError):
    pass


def _parse(data: bytes) -> ET.Element:
    # Uploaded XML: refuse DTDs/entities outright (billion-laughs); 3MF never needs them.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise SubsetError("3MF XML containing DOCTYPE/ENTITY declarations is not accepted")
    return ET.fromstring(data)


def _q(tag: str) -> str:
    return f"{{{NS_CORE}}}{tag}"


def _p(attr: str) -> str:
    return f"{{{NS_PROD}}}{attr}"


def _meta(el: ET.Element, key: str) -> Optional[str]:
    for m in el.findall("metadata"):
        if m.get("key") == key:
            return m.get("value")
    return None


def list_objects(src: str) -> list[dict]:
    """Objects in the 3MF: id, name, extruder, part summary, current instance count."""
    with zipfile.ZipFile(src) as z:
        settings = _parse(z.read(MODEL_SETTINGS))
        build = _parse(z.read(MODEL)).find(_q("build"))
    counts: dict[str, int] = {}
    for it in build.findall(_q("item")):
        counts[it.get("objectid")] = counts.get(it.get("objectid"), 0) + 1
    out = []
    for o in settings.findall("object"):
        oid = o.get("id")
        out.append({
            "id": int(oid),
            "name": _meta(o, "name") or f"object_{oid}",
            "extruder": _meta(o, "extruder"),
            "parts": [{"name": _meta(p, "name"), "subtype": p.get("subtype")} for p in o.findall("part")],
            "instances": counts.get(oid, 0),
            "overrides": sorted(
                m.get("key") for m in o.findall("metadata")
                if m.get("key") not in ("name", "extruder") and m.get("key")
            ),
        })
    return out


def plate_members(src: str) -> dict[int, dict[int, int]]:
    """{plate_no: {object_id: instances_on_that_plate}} as laid out in the source project."""
    with zipfile.ZipFile(src) as z:
        settings = _parse(z.read(MODEL_SETTINGS))
    out: dict[int, dict[int, int]] = {}
    for n, pl in enumerate(settings.findall("plate"), start=1):
        members = out.setdefault(int(_meta(pl, "plater_id") or n), {})
        for mi in pl.findall("model_instance"):
            oid = int(_meta(mi, "object_id"))
            members[oid] = members.get(oid, 0) + 1
    return out


def resolve_selection(objects: list[dict], selection: list[dict],
                      plates: Optional[dict[int, dict[int, int]]] = None) -> dict[int, int]:
    """Map selector list to {object_id: qty}.

    Selectors: ``{"id"|"name": ..., "qty": n}`` for one object, or ``{"plate": p, "qty": n}``
    for everything on source plate ``p`` (each object x its count on that plate x ``qty``).
    A name matching several objects is ambiguous (e.g. four distinct "Platte 1.stl") and
    is rejected with the candidate ids rather than guessed at.
    """
    by_id = {o["id"]: o for o in objects}
    want: dict[int, int] = {}
    for sel in selection:
        qty = int(sel.get("qty", 1))
        if qty < 1:
            raise SubsetError(f"qty must be >= 1 for {sel!r}")
        if "plate" in sel:
            pl = int(sel["plate"])
            if not (plates or {}).get(pl):
                raise SubsetError(f"No plate {pl} (or it is empty)")
            for oid, cnt in plates[pl].items():
                want[oid] = want.get(oid, 0) + cnt * qty
            continue
        if "id" in sel:
            oid = int(sel["id"])
            if oid not in by_id:
                raise SubsetError(f"No object with id {oid}")
        elif "name" in sel:
            hits = [o["id"] for o in objects if o["name"] == sel["name"]]
            if not hits:
                raise SubsetError(f"No object named {sel['name']!r}")
            if len(hits) > 1:
                raise SubsetError(f"Name {sel['name']!r} is ambiguous; use id (candidates: {hits})")
            oid = hits[0]
        else:
            raise SubsetError(f"Selector needs 'id', 'name' or 'plate': {sel!r}")
        want[oid] = want.get(oid, 0) + qty
    if not want:
        raise SubsetError("Selection is empty")
    return want


def _fmt(m: list[float]) -> str:
    return " ".join(repr(float(v)) if v != int(v) else str(int(v)) for v in m)


def build_subset(src: str, dst: str, want: dict[int, int],
                 printer_cfg: Optional[dict] = None) -> dict:
    """Write ``dst`` containing only ``want`` ({object_id: qty}), all on plate 1.

    ``printer_cfg`` (a resolved machine preset) retargets bed/printer keys in
    project_settings.config. Returns a small summary dict.
    """
    zin = zipfile.ZipFile(src)
    names = zin.namelist()
    keep_ids = {str(i) for i in want}

    model = _parse(zin.read(MODEL))
    # Orca rejects projects stamped by a newer Bambu Studio ("Unsupported 3MF version");
    # dropping the Application stamp is the same workaround the slice path uses.
    for md in model.findall(_q("metadata")):
        if md.get("name") == "Application":
            model.remove(md)
    resources = model.find(_q("resources"))
    build = model.find(_q("build"))

    # --- resources: drop unselected objects, remember which mesh files stay referenced
    kept_paths: set[str] = set()
    for obj in list(resources.findall(_q("object"))):
        if obj.get("id") not in keep_ids:
            resources.remove(obj)
            continue
        for comp in obj.iter(_q("component")):
            path = comp.get(_p("path"))
            if path:
                kept_paths.add(path.lstrip("/"))

    # --- build: qty instances per object, templated from its first existing item
    templates: dict[str, ET.Element] = {}
    for it in build.findall(_q("item")):
        if it.get("objectid") in keep_ids:
            templates.setdefault(it.get("objectid"), it)
        build.remove(it)
    missing = keep_ids - set(templates)
    if missing:
        raise SubsetError(f"Objects {sorted(missing)} have no build item in the source")
    instances: list[tuple[str, int]] = []
    for oid, qty in want.items():
        for k in range(qty):
            item = copy.deepcopy(templates[str(oid)])
            if item.get(_p("UUID")):
                item.set(_p("UUID"), str(uuid.uuid4()))
            build.append(item)
            instances.append((str(oid), k))

    # --- model_settings: filter objects, collapse to one plate, filter assemble
    ms = _parse(zin.read(MODEL_SETTINGS))
    for o in ms.findall("object"):
        if o.get("id") not in keep_ids:
            ms.remove(o)
    plates = ms.findall("plate")
    if plates:
        template = copy.deepcopy(plates[0])
        for pl in plates:
            ms.remove(pl)
        for child in list(template):
            if child.tag == "model_instance":
                template.remove(child)
            elif child.tag == "metadata" and child.get("key") in (
                    "thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file"):
                template.remove(child)
        for n, (oid, k) in enumerate(instances):
            mi = ET.SubElement(template, "model_instance")
            for key, val in (("object_id", oid), ("instance_id", str(k)),
                             ("identify_id", str(1000 + n))):
                ET.SubElement(mi, "metadata", {"key": key, "value": val})
        # new plate goes before <assemble> (Orca tolerates any order; keep file tidy)
        ms.insert(len(ms.findall("object")), template)
    asm = ms.find("assemble")
    if asm is not None:
        for ai in list(asm):
            if ai.get("object_id") not in keep_ids:
                asm.remove(ai)

    # --- rels: keep only references to retained mesh files. Text-level edit: ElementTree
    # would re-prefix the (default) rels namespace as ns0:, which Orca's loader rejects.
    rels = None
    if MODEL_RELS in names:
        def _keep_rel(m: "re.Match") -> str:
            tgt = re.search(r'Target="([^"]*)"', m.group(0))
            t = tgt.group(1).lstrip("/") if tgt else ""
            return m.group(0) if not t.startswith("3D/Objects/") or t in kept_paths else ""
        rels = re.sub(r"<Relationship [^>]*/>\s*", _keep_rel,
                      zin.read(MODEL_RELS).decode("utf-8")).encode("utf-8")

    project = zin.read(PROJECT_SETTINGS) if PROJECT_SETTINGS in names else None
    if project is not None:
        cfg = json.loads(project)
        for k, floor in _ORCA_MIN_CLAMP.items():
            try:
                if float(cfg.get(k, floor)) < floor:
                    cfg[k] = str(floor)
            except (TypeError, ValueError):
                pass
        if printer_cfg is not None:
            for k in PRINTER_KEYS:
                if k in printer_cfg:
                    cfg[k] = printer_cfg[k]
            if printer_cfg.get("name"):
                cfg["printer_settings_id"] = printer_cfg["name"]
            # The process preset embedded in the project lists the printers it is valid for;
            # Orca refuses to arrange/slice ("process not compatible with printer") otherwise.
            cfg["print_compatible_printers"] = []
        project = json.dumps(cfg, indent=4, ensure_ascii=False).encode()

    def ser(el: ET.Element) -> bytes:
        return ET.tostring(el, encoding="UTF-8", xml_declaration=True)

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            n = info.filename
            if n == MODEL:
                data = ser(model)
            elif n == MODEL_SETTINGS:
                data = ser(ms)
            elif n == MODEL_RELS and rels is not None:
                data = rels
            elif n == PROJECT_SETTINGS and project is not None:
                data = project
            elif n.startswith("3D/Objects/") and n not in kept_paths:
                continue
            elif _STALE.match(n):
                continue
            elif n in _PER_OBJECT_FILES:
                root = _parse(zin.read(n))
                for o in list(root.findall("object")):
                    if o.get("id") not in keep_ids:
                        root.remove(o)
                data = ser(root)
            else:
                with zin.open(info) as f:
                    # stream big mesh files rather than materialising them twice
                    zout.writestr(info.filename, f.read(), zipfile.ZIP_DEFLATED)
                continue
            zout.writestr(n, data)
    zin.close()
    return {"objects": len(want), "instances": len(instances), "mesh_files": sorted(kept_paths)}


def count_plates(path: str) -> int:
    with zipfile.ZipFile(path) as z:
        return len(_parse(z.read(MODEL_SETTINGS)).findall("plate"))


# ----------------------------------------------------------- merge several subsets
#
# Each source is first reduced with build_subset(); merge_subsets() then folds the extra
# subsets into the first one (the "base"). Per-object data travels with the object (ids
# renumbered, mesh files renamed on collision); project-wide data (process / filament /
# printer settings, thumbnails, Auxiliaries) comes from the base only.

_OBJECT_ID_ATTR = re.compile(rb'(<object\b[^>]*?\bid=")(\d+)(")')
_REL_MODEL_TYPE ="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"


def _filament_count(cfg: dict) -> int:
    fc = cfg.get("filament_colour")
    return len(fc) if isinstance(fc, list) else 1


def merge_subsets(parts: list[str], dst: str, labels: Optional[list[str]] = None) -> dict:
    """Merge subset files (build_subset output) into ``dst``; ``parts[0]`` is the base.

    Returns {"objects", "instances", "warnings": [str]}.
    """
    if not parts:
        raise SubsetError("Nothing to merge")
    labels = labels or [f"source {i}" for i in range(len(parts))]
    warnings: list[str] = []
    zb = zipfile.ZipFile(parts[0])
    base_names = zb.namelist()
    model = _parse(zb.read(MODEL))
    ms = _parse(zb.read(MODEL_SETTINGS))
    resources = model.find(_q("resources"))
    build = model.find(_q("build"))
    asm = ms.find("assemble")
    base_cfg = json.loads(zb.read(PROJECT_SETTINGS)) if PROJECT_SETTINGS in base_names else {}
    n_fil = _filament_count(base_cfg)
    side = {n: _parse(zb.read(n)) for n in _PER_OBJECT_FILES if n in base_names}
    rels_text = zb.read(MODEL_RELS).decode("utf-8") if MODEL_RELS in base_names else None
    taken = set(base_names)
    used_ids = {int(o.get("id")) for o in resources.findall(_q("object"))}
    new_files: dict[str, bytes] = {}
    new_rels: list[str] = []
    inner_taken: set[int] = set()
    for c in resources.iter(_q("component")):
        if c.get(_p("path")):
            inner_taken.add(int(c.get("objectid")))
    for o in ms.findall("object"):
        inner_taken |= {int(p.get("id")) for p in o.findall("part")}

    for idx, path in enumerate(parts[1:], start=1):
        label = labels[idx]
        with zipfile.ZipFile(path) as zp:
            pmodel = _parse(zp.read(MODEL))
            pms = _parse(zp.read(MODEL_SETTINGS))
            pcfg = json.loads(zp.read(PROJECT_SETTINGS)) if PROJECT_SETTINGS in zp.namelist() else {}
            # project-wide settings come from the base; say so where this source differed
            for key in ("print_settings_id", "filament_settings_id"):
                if pcfg.get(key) != base_cfg.get(key):
                    warnings.append(f"{label}: {key} {pcfg.get(key)!r} differs from base "
                                    f"{base_cfg.get(key)!r}; base project settings are used")
            for o in pms.findall("object"):
                ext = _meta(o, "extruder")
                if ext and ext.isdigit() and int(ext) > n_fil:
                    raise SubsetError(f"{label}: object {o.get('id')} uses filament/extruder "
                                      f"{ext} but the base project only has {n_fil}")
            if _filament_count(pcfg) != n_fil and any(
                    sum(v["paint"].values()) for v in fingerprint(path).values()):
                warnings.append(f"{label}: has painted data but {_filament_count(pcfg)} filament "
                                f"slots vs base {n_fil}; painted filament indices are not remapped")

            remap: dict[str, str] = {}
            for o in pmodel.find(_q("resources")).findall(_q("object")):
                remap[o.get("id")] = str(max(used_ids | {0}) + 1)
                used_ids.add(int(remap[o.get("id")]))

            # Inner ids (the <object id> inside a mesh file == component objectid == model_settings
            # <part id>) must be unique across the *whole* merged project: OrcaSlicer resolves
            # parts by id alone, so two sources both using e.g. id 5 make it attach the wrong
            # mesh (the merged pots floated 60 mm above the bed). Renumber every non-base one.
            def _fresh() -> str:
                v = max(inner_taken | used_ids | {0}) + 1
                inner_taken.add(v)
                return str(v)

            inner_map: dict[tuple[str, str], str] = {}      # (mesh path, old id) -> new id
            part_map: dict[str, str] = {}                   # old part id -> new id (first mesh wins)
            for c in pmodel.iter(_q("component")):
                key = (c.get(_p("path")) or "", c.get("objectid"))
                if key[0] and key not in inner_map:
                    inner_map[key] = _fresh()
                    part_map.setdefault(key[1], inner_map[key])
            for o in pms.findall("object"):
                for part in o.findall("part"):
                    if part.get("id") not in part_map:
                        part_map[part.get("id")] = _fresh()

            # mesh files: every part-source mesh is renamed on collision with an existing name
            path_map: dict[str, str] = {}
            for comp in pmodel.iter(_q("component")):
                cp = comp.get(_p("path"))
                if cp and cp not in path_map:
                    rel = cp.lstrip("/")
                    new = rel
                    if new in taken:
                        stem, _, ext = rel.rpartition(".")
                        new = f"{stem}__m{idx}.{ext}"
                    taken.add(new)
                    path_map[cp] = "/" + new
                    new_files[new] = _OBJECT_ID_ATTR.sub(
                        lambda m, cp=cp: m.group(1) + inner_map.get(
                            (cp, m.group(2).decode()), m.group(2).decode()).encode() + m.group(3),
                        zp.read(rel))
                    new_rels.append(f'<Relationship Target="/{new}" Id="rel-m{idx}-{len(new_rels)}" '
                                    f'Type="{_REL_MODEL_TYPE}"/>')

            for o in pmodel.find(_q("resources")).findall(_q("object")):
                o.set("id", remap[o.get("id")])
                for comp in o.iter(_q("component")):
                    cp = comp.get(_p("path"))
                    if cp:
                        comp.set(_p("path"), path_map[cp])
                        comp.set("objectid", inner_map[(cp, comp.get("objectid"))])
                    elif comp.get("objectid") in remap:
                        comp.set("objectid", remap[comp.get("objectid")])
                resources.append(o)
            for it in pmodel.find(_q("build")).findall(_q("item")):
                it.set("objectid", remap[it.get("objectid")])
                build.append(it)
            for o in pms.findall("object"):
                o.set("id", remap[o.get("id")])
                for part in o.findall("part"):
                    part.set("id", part_map[part.get("id")])
                ms.insert(len(ms.findall("object")), o)
            pasm = pms.find("assemble")
            if pasm is not None:
                if asm is None:
                    asm = ET.SubElement(ms, "assemble")
                for ai in pasm:
                    if ai.get("object_id") in remap:
                        ai.set("object_id", remap[ai.get("object_id")])
                        asm.append(ai)
            for n in _PER_OBJECT_FILES:
                if n in zp.namelist():
                    proot = _parse(zp.read(n))
                    for o in proot.findall("object"):
                        if o.get("id") in remap:
                            o.set("id", remap[o.get("id")])
                            side.setdefault(n, ET.Element(proot.tag, proot.attrib)).append(o)

    if rels_text is not None and new_rels:
        rels_text = rels_text.replace("</Relationships>", " " + "\n ".join(new_rels) + "\n</Relationships>")

    # one plate carrying every instance (the arranger spreads them afterwards)
    instances: list[tuple[str, int]] = []
    seen: dict[str, int] = {}
    for it in build.findall(_q("item")):
        oid = it.get("objectid")
        instances.append((oid, seen.get(oid, 0)))
        seen[oid] = seen.get(oid, 0) + 1
    plates = ms.findall("plate")
    if plates:
        for pl in plates[1:]:
            ms.remove(pl)
        for mi in plates[0].findall("model_instance"):
            plates[0].remove(mi)
        for n, (oid, k) in enumerate(instances):
            mi = ET.SubElement(plates[0], "model_instance")
            for key, val in (("object_id", oid), ("instance_id", str(k)),
                             ("identify_id", str(1000 + n))):
                ET.SubElement(mi, "metadata", {"key": key, "value": val})

    def ser(el: ET.Element) -> bytes:
        return ET.tostring(el, encoding="UTF-8", xml_declaration=True)

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in base_names:
            if n == MODEL:
                data = ser(model)
            elif n == MODEL_SETTINGS:
                data = ser(ms)
            elif n == MODEL_RELS and rels_text is not None:
                data = rels_text.encode("utf-8")
            elif n in side:
                data = ser(side[n])
            else:
                data = zb.read(n)
            zout.writestr(n, data, zipfile.ZIP_DEFLATED)
        for n, root in side.items():
            if n not in base_names:
                zout.writestr(n, ser(root), zipfile.ZIP_DEFLATED)
        for n, data in new_files.items():
            zout.writestr(n, data, zipfile.ZIP_DEFLATED)
    zb.close()
    return {"objects": len(resources.findall(_q("object"))), "instances": len(instances),
            "warnings": warnings}


# ----------------------------------------------------------- layout via oracle
#
# OrcaSlicer's own export of an arranged file is lossy: instances that end up on
# different plates (or rotated differently) are split into clone objects that lose their
# name, per-object overrides and even extruder. So Orca is used purely as a layout
# oracle on an "expanded" copy where every instance is its own uniquely named object
# (nothing to clone), and the resulting plate/transform layout is transplanted back
# into the byte-intact subset.

_TAG = "lmns:"


def expand_instances(subset: str, dst: str) -> None:
    """Oracle input: one uniquely named object per instance of ``subset``."""
    with zipfile.ZipFile(subset) as zin:
        model = _parse(zin.read(MODEL))
        ms = _parse(zin.read(MODEL_SETTINGS))
        resources = model.find(_q("resources"))
        build = model.find(_q("build"))
        res_by_id = {o.get("id"): o for o in resources.findall(_q("object"))}
        set_by_id = {o.get("id"): o for o in ms.findall("object")}
        items = build.findall(_q("item"))
        for o in list(res_by_id.values()):
            resources.remove(o)
        for o in list(set_by_id.values()):
            ms.remove(o)
        for it in items:
            build.remove(it)
        seen: dict[str, int] = {}
        new_ids: list[str] = []
        for n, it in enumerate(items):
            oid = it.get("objectid")
            k = seen.get(oid, 0)
            seen[oid] = k + 1
            nid = str(1000 + n)
            new_ids.append(nid)
            r = copy.deepcopy(res_by_id[oid])
            r.set("id", nid)
            if r.get(_p("UUID")):
                r.set(_p("UUID"), str(uuid.uuid4()))
            resources.append(r)
            so = copy.deepcopy(set_by_id[oid])
            so.set("id", nid)
            for m in so.findall("metadata"):
                if m.get("key") == "name":
                    m.set("value", f"{_TAG}{oid}:{k}")
            ms.insert(len(ms.findall("object")), so)
            it.set("objectid", nid)
            build.append(it)
        for pl in ms.findall("plate"):
            for mi in pl.findall("model_instance"):
                pl.remove(mi)
            for nid in new_ids:
                mi = ET.SubElement(pl, "model_instance")
                for key, val in (("object_id", nid), ("instance_id", "0"), ("identify_id", str(3000 + int(nid)))):
                    ET.SubElement(mi, "metadata", {"key": key, "value": val})
        asm = ms.find("assemble")
        if asm is not None:
            ms.remove(asm)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename == MODEL:
                    zout.writestr(info.filename, ET.tostring(model, encoding="UTF-8", xml_declaration=True))
                elif info.filename == MODEL_SETTINGS:
                    zout.writestr(info.filename, ET.tostring(ms, encoding="UTF-8", xml_declaration=True))
                elif info.filename in _PER_OBJECT_FILES:
                    continue  # keyed by the old object ids; irrelevant to layout
                else:
                    with zin.open(info) as f:
                        zout.writestr(info.filename, f.read(), zipfile.ZIP_DEFLATED)


def _arranged_layout(path: str) -> tuple[dict[tuple[str, int], str], list[list[tuple[str, int]]]]:
    """({(obj, idx): transform}, plates[[(obj, idx)]]) read from an oracle output."""
    with zipfile.ZipFile(path) as z:
        model = _parse(z.read(MODEL))
        ms = _parse(z.read(MODEL_SETTINGS))
    tag_of: dict[str, tuple[str, int]] = {}
    for o in ms.findall("object"):
        name = _meta(o, "name") or ""
        if not name.startswith(_TAG):
            raise SubsetError(f"Arranged object {o.get('id')} lost its tag (name={name!r})")
        oid, k = name[len(_TAG):].split(":")
        tag_of[o.get("id")] = (oid, int(k))
    transforms: dict[tuple[str, int], str] = {}
    for it in model.find(_q("build")).findall(_q("item")):
        key = tag_of[it.get("objectid")]
        if key in transforms:
            raise SubsetError(f"Arranged file has two items for {key}")
        transforms[key] = it.get("transform", "")
    plates = [[tag_of[_meta(mi, "object_id")] for mi in pl.findall("model_instance")]
              for pl in ms.findall("plate")]
    return transforms, plates


def transplant_layout(subset: str, arranged: str, dst: str) -> int:
    """Write ``subset`` to ``dst`` with plate lists and instance transforms taken from
    the oracle output ``arranged``. Returns the plate count."""
    a_tf, a_plates = _arranged_layout(arranged)
    placed = sorted(x for p in a_plates for x in p)
    if placed != sorted(a_tf) or len(set(placed)) != len(placed):
        raise SubsetError("Arranged plates do not cover every instance exactly once")

    with zipfile.ZipFile(subset) as zin:
        model = _parse(zin.read(MODEL))
        ms = _parse(zin.read(MODEL_SETTINGS))
        seen: dict[str, int] = {}
        expected: set[tuple[str, int]] = set()
        for it in model.find(_q("build")).findall(_q("item")):
            oid = it.get("objectid")
            k = seen.get(oid, 0)
            seen[oid] = k + 1
            expected.add((oid, k))
            if (oid, k) not in a_tf:
                raise SubsetError(f"Instance {(oid, k)} missing from arranged file")
            it.set("transform", a_tf[(oid, k)])
        if expected != set(a_tf):
            raise SubsetError("Arranged file contains instances not in the subset")
        template = ms.find("plate")
        insert_at = list(ms).index(template)
        ms.remove(template)
        for child in list(template):
            if child.tag == "model_instance":
                template.remove(child)
        for n, plate in enumerate(a_plates):
            pl = copy.deepcopy(template)
            for m in pl.findall("metadata"):
                if m.get("key") == "plater_id":
                    m.set("value", str(n + 1))
                elif m.get("key") == "plater_name":
                    m.set("value", "")
            for j, (oid, k) in enumerate(plate):
                mi = ET.SubElement(pl, "model_instance")
                for key, val in (("object_id", oid), ("instance_id", str(k)),
                                 ("identify_id", str(2000 + n * 1000 + j))):
                    ET.SubElement(mi, "metadata", {"key": key, "value": val})
            ms.insert(insert_at + n, pl)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename == MODEL:
                    zout.writestr(info.filename, ET.tostring(model, encoding="UTF-8", xml_declaration=True))
                elif info.filename == MODEL_SETTINGS:
                    zout.writestr(info.filename, ET.tostring(ms, encoding="UTF-8", xml_declaration=True))
                else:
                    with zin.open(info) as f:
                        zout.writestr(info.filename, f.read(), zipfile.ZIP_DEFLATED)
    return len(a_plates)


# ---------------------------------------------------------------- verification

_PAINT = ("paint_color", "paint_supports", "paint_seam", "paint_fuzzy_skin", "mmu_segmentation")


def fingerprint(path: str) -> dict:
    """Semantic summary used to prove overrides survived: per object name -> {...}.

    Independent of ids/ordering/serialization so it can compare the pre-arrange subset
    and Orca's re-exported output.
    """
    with zipfile.ZipFile(path) as z:
        ms = _parse(z.read(MODEL_SETTINGS))
        model = _parse(z.read(MODEL))
        ranges = {}
        if "Metadata/layer_config_ranges.xml" in z.namelist():
            for o in _parse(z.read("Metadata/layer_config_ranges.xml")).findall("object"):
                ranges[o.get("id")] = ET.tostring(o).decode()
        comp_path: dict[str, list[str]] = {}
        for obj in model.find(_q("resources")).findall(_q("object")):
            comp_path[obj.get("id")] = [
                c.get(_p("path"), "").lstrip("/") for c in obj.iter(_q("component"))]
        inst: dict[str, int] = {}
        for it in model.find(_q("build")).findall(_q("item")):
            inst[it.get("objectid")] = inst.get(it.get("objectid"), 0) + 1
        out: dict[str, dict] = {}
        for o in ms.findall("object"):
            oid = o.get("id")
            paint = {k: 0 for k in _PAINT}
            for p in comp_path.get(oid, []):
                if p in z.namelist():
                    with z.open(p) as f:
                        data = f.read()  # bytes.count is ~100x faster than regex on decoded text
                    for k in _PAINT:
                        paint[k] += data.count(k.encode() + b'="')
            key = f"{_meta(o, 'name')}#{oid}"
            out[key] = {
                "meta": sorted((m.get("key"), m.get("value")) for m in o.findall("metadata")
                               if m.get("key") not in (None, "face_count")),
                "parts": sorted(
                    (p.get("subtype"), _meta(p, "name"),
                     tuple(sorted((m.get("key"), m.get("value")) for m in p.findall("metadata")
                                  if m.get("key") not in (None, "matrix", "source_offset_x", "source_offset_y",
                                                          "source_offset_z", "source_object_id",
                                                          "source_volume_id", "source_file"))))
                    for p in o.findall("part")),
                "paint": paint,
                "ranges": ranges.get(oid),
                "instances": inst.get(oid, 0),
            }
        return out
