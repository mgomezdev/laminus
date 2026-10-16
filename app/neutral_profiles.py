"""Neutral (model / nozzle-variant) views over a ProfileCatalog.

Callers such as Themis know a printer by (manufacturer, model, nozzle variant), not by
OrcaSlicer preset name or per-build UUID. Everything here is a pure function of the
already-built catalog - no engine, no filesystem - so it can be answered from a cached
catalog while OrcaSlicer is down.

Refs are `<kind>:<slug of preset name>` (e.g. `machine:bambu-lab-p1s-0.4-nozzle`), so they
survive catalog rebuilds for as long as the preset keeps its name.
"""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Optional

from app.profile_catalog import _first_or_default, parse_machine_name

_HIGH_FLOW_RE = re.compile(r"\s*\bhigh[\s_-]?flow\b", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9.]+")

MAX_SUGGESTIONS = 20
MAX_VENDOR_ONLY = 5
_BED_TOLERANCE_MM = 1.0


def make_ref(kind: str, name: str) -> str:
    return f"{kind}:{_SLUG_RE.sub('-', name.lower()).strip('-')}"


def _norm(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _to_float(raw) -> Optional[float]:
    try:
        return float(_first_or_default(raw, None))
    except (TypeError, ValueError):
        return None


# Vendor-neutral preset library shipped with OrcaSlicer; its universal presets apply to any machine.
_SHARED_VENDORS = {"OrcaFilamentLibrary"}


def _vendor_dir(entry: dict) -> str:
    return entry["rel_path"].split("/", 1)[0]


def _universal_applies(machine: dict, preset: dict) -> bool:
    """A preset with no `compatible_printers` is universal *within its vendor bundle*
    (plus user presets and the shared library), not across every vendor in the tree."""
    if preset.get("source") == "user" or _vendor_dir(preset) in _SHARED_VENDORS:
        return True
    return machine.get("source") == preset.get("source") and _vendor_dir(machine) == _vendor_dir(preset)


def _instantiable(entry: dict) -> bool:
    """Orca marks abstract bases (fdm_process_common, ...) `instantiation: "false"`; they
    are inheritance parents, not selectable presets."""
    return str(entry.get("instantiation", "true")).lower() != "false"


class MachineInfo:
    """Neutral facts about one machine entry."""

    __slots__ = ("entry", "ref", "manufacturer", "model", "nozzle_mm", "flow")

    def __init__(self, entry: dict):
        self.entry = entry
        name = entry["name"]
        self.ref = make_ref("machine", name)
        # "... 0.4 High Flow nozzle" does not match parse_machine_name as-is.
        self.flow = "high_flow" if _HIGH_FLOW_RE.search(name) else "standard"
        parsed = parse_machine_name(_HIGH_FLOW_RE.sub("", name))
        if parsed:
            self.manufacturer, self.model, nozzle = parsed
        else:
            self.manufacturer, self.model, nozzle = "", name, None
        self.nozzle_mm = _to_float(entry.get("nozzle_diameter")) or _to_float(nozzle)

    def is_default_candidate(self) -> bool:
        return self.flow == "standard" and self.nozzle_mm == 0.4

    def sort_key(self):
        return (self.nozzle_mm if self.nozzle_mm is not None else 99.0, self.flow != "standard", self.entry["name"])


def _vendor_match(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na and nb and (na in nb or nb in na))


def _query_keys(manufacturer: str, model: str, model_key: Optional[str]) -> list[str]:
    """Normalised model strings to compare against, with a leading manufacturer stripped
    ("Bambu Lab P1S" -> "p1s")."""
    nm = _norm(manufacturer)
    keys = []
    for raw in (model, model_key):
        k = _norm(raw)
        if nm and k.startswith(nm) and len(k) > len(nm):
            k = k[len(nm):]
        if k:
            keys.append(k)
    return keys


def _score(info: MachineInfo, manufacturer: str, keys: list[str]) -> Optional[tuple[float, str]]:
    vendor = _vendor_match(manufacturer, info.manufacturer)
    nm = _norm(info.model)
    if keys and nm in keys:
        if vendor:
            return (1.0 if nm == keys[0] else 0.95), "exact_model"
        return 0.5, "fuzzy_name"
    best = 0.0
    for k in keys:
        ratio = SequenceMatcher(None, k, nm).ratio()
        if min(len(k), len(nm)) >= 3 and (k in nm or nm in k):
            ratio = max(ratio, 0.8)
        best = max(best, ratio)
    if vendor and best >= 0.75:
        return round(best * 0.9, 2), "fuzzy_name"
    if not vendor and best >= 0.85:
        return round(best * 0.6, 2), "fuzzy_name"
    if vendor:
        return 0.1, "vendor_only"
    return None


class NeutralIndex:
    """Lazy ref/model indexes over one ProfileCatalog instance."""

    def __init__(self, catalog):
        self._catalog = catalog
        self.machines = [MachineInfo(e) for e in catalog._catalog["machine"] if _instantiable(e)]
        self.machine_by_ref = {m.ref: m for m in self.machines}
        self._machine_by_name = {m.entry["name"]: m for m in self.machines}
        self._version: Optional[str] = None

    # ---- version ----

    @property
    def version(self) -> str:
        """Opaque; changes whenever any preset's identity or compatibility data changes."""
        if self._version is None:
            h = hashlib.sha1()
            for ptype in ("machine", "process", "filament"):
                rows = sorted(
                    json.dumps(
                        [e["uuid"], e["name"], e.get("compatible_printers"), e.get("nozzle_diameter"),
                         e.get("bed_size_x"), e.get("bed_size_y")],
                        sort_keys=True, default=str,
                    )
                    for e in self._catalog._catalog[ptype]
                )
                h.update(ptype.encode())
                h.update("\n".join(rows).encode())
            self._version = h.hexdigest()[:16]
        return self._version

    # ---- machines ----

    @staticmethod
    def _machine_json(m: MachineInfo) -> dict:
        return {
            "ref": m.ref, "uuid": m.entry["uuid"], "name": m.entry["name"],
            "nozzle_diameter_mm": m.nozzle_mm, "flow": m.flow,
        }

    def suggest_machines(self, manufacturer: str, model: str, model_key: Optional[str]) -> list[dict]:
        keys = _query_keys(manufacturer, model, model_key)
        scored = []
        for m in self.machines:
            s = _score(m, manufacturer, keys)
            if s is not None:
                scored.append((s, m))
        scored.sort(key=lambda t: (-t[0][0], not t[1].is_default_candidate(), t[1].sort_key()))
        out, vendor_only = [], 0
        for (conf, reason), m in scored:
            if reason == "vendor_only":
                if vendor_only >= MAX_VENDOR_ONLY:
                    continue
                vendor_only += 1
            out.append({**self._machine_json(m), "confidence": conf, "match_reason": reason})
            if len(out) >= MAX_SUGGESTIONS:
                break
        return out

    def model_variants(self, manufacturer: str, model: str, model_key: Optional[str]) -> list[dict]:
        """Nozzle variants of the single best-matching (manufacturer, model) group."""
        keys = _query_keys(manufacturer, model, model_key)
        groups: dict[tuple[str, str], list[MachineInfo]] = {}
        best_conf: dict[tuple[str, str], float] = {}
        for m in self.machines:
            s = _score(m, manufacturer, keys)
            if s is None or s[1] == "vendor_only":
                continue
            g = (_norm(m.manufacturer), _norm(m.model))
            groups.setdefault(g, []).append(m)
            best_conf[g] = max(best_conf.get(g, 0.0), s[0])
        if not groups:
            return []
        best = max(groups, key=lambda g: best_conf[g])
        variants = sorted(groups[best], key=MachineInfo.sort_key)
        default = next((v for v in variants if v.is_default_candidate()), None)
        if default is None:
            default = next((v for v in variants if v.flow == "standard"), variants[0])
        return [{**self._machine_json(v), "is_default_guess": v is default} for v in variants]

    # ---- presets ----

    def _compatible_models(self, entry: dict) -> list[str]:
        models = set()
        for printer in entry.get("compatible_printers") or []:
            m = self._machine_by_name.get(printer)
            if m is not None and m.manufacturer:
                models.add(f"{m.manufacturer} {m.model}")
        return sorted(models)

    def machine_presets(self, machine: MachineInfo, kinds: tuple[str, ...]) -> list[dict]:
        name = machine.entry["name"]
        out: dict[str, dict] = {}
        for kind in kinds:
            for e in self._catalog._catalog[kind]:
                compat = e.get("compatible_printers") or []
                if not _instantiable(e):
                    continue
                if compat:
                    if name not in compat:
                        continue
                elif not _universal_applies(machine.entry, e):
                    continue
                item = {"ref": make_ref(kind, e["name"]), "uuid": e["uuid"], "name": e["name"], "kind": kind}
                models = self._compatible_models(e)
                if models:
                    item["compatible_models"] = models
                out[item["ref"]] = item  # same name in several vendor dirs: one ref, last (user) wins
        return list(out.values())

    # ---- supports ----

    def supports(self, ref: str, bed_x: Optional[float], bed_y: Optional[float]) -> tuple[bool, Optional[str]]:
        m = self.machine_by_ref.get(ref)
        if m is None:
            return False, "unknown_machine_ref"
        for want, have in ((bed_x, m.entry.get("bed_size_x")), (bed_y, m.entry.get("bed_size_y"))):
            have_f = _to_float(have)
            # A machine that doesn't declare a bed size can't be ruled out.
            if want is not None and have_f is not None and abs(want - have_f) > _BED_TOLERANCE_MM:
                return False, "bed_mismatch"
        return True, None
