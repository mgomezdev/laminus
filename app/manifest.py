"""Per-plate manifest embedded in packed / subset / merged 3MFs (BIZ-359).

``Metadata/laminus_manifest.json`` says, for every plate of the returned project, which
request item and which part each build instance came from::

    {"version": 1,
     "items": [{"item": 0, "source": "a.stl", "requested": 5}, ...],
     "plates": [{"plate": 1, "instances": [
         {"item": 0, "source": "a.stl", "object_id": "4", "object_name": "a", "part": "a.stl"}]}]}

``item`` is the 0-based index of the request item (``/pack`` ``items`` entry, or upload
order without ``items``) or ``selection`` entry. ``part`` is a stable key within the item:
the STL filename, or the source 3MF object id. ``plate`` is the plater_id, i.e. the N a later
``POST /api/v1/jobs plate=N`` slices. Instances come from the plate lists actually written
into ``model_settings.config``; a manifest whose per-item totals disagree with what was
requested is an error, never returned.
"""
from __future__ import annotations

import json
import zipfile
from typing import Callable, Optional

from app import subset_3mf as _subset

MANIFEST = "Metadata/laminus_manifest.json"
VERSION = 1

# origin(object_id, instance_index) -> {"item", "source", "part"} or None
Origin = Callable[[str, int], Optional[dict]]


class ManifestError(ValueError):
    """The output's plates do not account for exactly the requested instances."""


def _plates(z: zipfile.ZipFile) -> tuple[list[tuple[int, list[tuple[str, int]]]], dict[str, str]]:
    ms = _subset._parse(z.read(_subset.MODEL_SETTINGS))
    names = {o.get("id"): _subset._meta(o, "name") or f"object_{o.get('id')}"
             for o in ms.findall("object")}
    plates = []
    for n, pl in enumerate(ms.findall("plate"), start=1):
        members = []
        for mi in pl.findall("model_instance"):
            members.append((_subset._meta(mi, "object_id"), int(_subset._meta(mi, "instance_id") or 0)))
        plates.append((int(_subset._meta(pl, "plater_id") or n), members))
    return plates, names


def build_manifest(path: str, origin: Origin, requested: list[dict]) -> dict:
    """Manifest for the project at *path*; *requested* = [{"item", "source", "requested"}]."""
    with _subset.open_zip(path) as z:
        plates, names = _plates(z)
    want = {r["item"]: r["requested"] for r in requested}
    got: dict[int, int] = {}
    seen: set[tuple[str, int]] = set()
    out = []
    for plate_no, members in plates:
        insts = []
        for oid, k in members:
            if (oid, k) in seen:
                raise ManifestError(f"object {oid} instance {k} is on more than one plate")
            seen.add((oid, k))
            o = origin(oid, k)
            if o is None:
                raise ManifestError(f"object {oid} instance {k} on plate {plate_no} has no known origin")
            got[o["item"]] = got.get(o["item"], 0) + 1
            insts.append({"item": o["item"], "source": o["source"], "object_id": oid,
                          "object_name": names.get(oid, f"object_{oid}"), "part": o["part"]})
        out.append({"plate": plate_no, "instances": insts})
    if got != want:
        raise ManifestError(f"plates hold {dict(sorted(got.items()))} instances per item, "
                            f"requested {dict(sorted(want.items()))}")
    return {"version": VERSION, "items": requested, "plates": out}


def embed_manifest(path: str, manifest: dict) -> None:
    """Write *manifest* into the 3MF at *path* (in place), replacing any earlier one."""
    with zipfile.ZipFile(path) as z:
        entries = [(info, z.read(info.filename)) for info in z.infolist() if info.filename != MANIFEST]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in entries:
            out.writestr(info, data)
        out.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1))


def read_manifest(path: str) -> Optional[dict]:
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read(MANIFEST)) if MANIFEST in z.namelist() else None


def requested_from(origins: dict) -> list[dict]:
    """[{"item", "source", "requested"}] counted from a {key: [origin dict, ...]} map."""
    acc: dict[int, dict] = {}
    for lst in origins.values():
        for o in lst:
            r = acc.setdefault(o["item"], {"item": o["item"], "source": o["source"], "requested": 0})
            r["requested"] += 1
    return [acc[i] for i in sorted(acc)]


def keyed_origin(origins: dict[str, list[dict]]) -> Origin:
    """Origin for projects whose instance k of object oid is the k-th entry of origins[oid]."""
    def _o(oid: str, k: int) -> Optional[dict]:
        lst = origins.get(oid)
        return lst[k] if lst is not None and 0 <= k < len(lst) else None
    return _o


def pack_origin(path: str, per_input: list[dict]) -> Origin:
    """Origin for an Orca ``/pack`` export: every input copy is its own object and Orca
    records its input position as the part's ``source_object_id`` (the same mapping the
    min-Z restore relies on)."""
    with _subset.open_zip(path) as z:
        ms = _subset._parse(z.read(_subset.MODEL_SETTINGS))
    source_of: dict[str, int] = {}
    for o in ms.findall("object"):
        for part in o.findall("part"):
            v = _subset._meta(part, "source_object_id")
            if v is not None and v.isdigit():
                source_of[o.get("id")] = int(v)
                break

    def _o(oid: str, k: int) -> Optional[dict]:
        src = source_of.get(oid)
        if k != 0 or src is None or not 0 <= src < len(per_input):
            return None
        return per_input[src]
    return _o
