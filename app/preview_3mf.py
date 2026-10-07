"""Per-object preview thumbnails for an OrcaSlicer/Bambu 3MF.

The 3MF only carries per-*plate* renders, so the object itself is rasterised here:
orthographic three-quarter view, flat shading, z-buffered point sampling (numpy only, no
GPU / no slicer). Modifier / negative / support-blocker parts are left out; the object is
tinted with its extruder's filament colour. Output is a transparent PNG.
"""
from __future__ import annotations

import base64
import re
import struct
import zipfile
import zlib
from typing import Optional

import numpy as np

from app.subset_3mf import (
    MODEL, MODEL_SETTINGS, PROJECT_SETTINGS, SubsetError, _meta, _p, _parse, _q,
)

_NON_PRINTED_PARTS = {"modifier_part", "negative_part", "support_blocker", "support_enforcer"}
_SUPERSAMPLE = 2
_CLUSTER_ABOVE_TRIS = 300_000          # decimate huge meshes by vertex clustering
_MAX_CHUNK_SAMPLES = 2_000_000
_DEFAULT_RGB = (158, 170, 188)

_VERT_TAG = re.compile(rb'<vertex x="([^"]*)" y="([^"]*)" z="([^"]*)"[^>]*/>')
_TRI_TAG = re.compile(rb'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"[^>]*/>')
_OBJ_BLOCK = re.compile(rb'<object\b[^>]*\bid="(\d+)"[^>]*>(.*?)</object>', re.S)


# ------------------------------------------------------------------ mesh loading

def _mesh_from_block(block: bytes) -> Optional[tuple[np.ndarray, np.ndarray]]:
    v0, v1 = block.find(b"<vertices"), block.find(b"</vertices>")
    t0, t1 = block.find(b"<triangles"), block.find(b"</triangles>")
    if min(v0, v1, t0, t1) < 0:
        return None
    vtxt = _VERT_TAG.sub(rb"\1 \2 \3 ", block[v0:v1])
    ttxt = _TRI_TAG.sub(rb"\1 \2 \3 ", block[t0:t1])
    vtxt = re.sub(rb"<[^>]*>", b" ", vtxt)
    ttxt = re.sub(rb"<[^>]*>", b" ", ttxt)
    verts = np.fromstring(vtxt, sep=" ", dtype=np.float64)
    tris = np.fromstring(ttxt, sep=" ", dtype=np.int64)
    if verts.size < 9 or tris.size < 3:
        return None
    return verts.reshape(-1, 3), tris.reshape(-1, 3)


def _mats(transform: Optional[str]) -> tuple[np.ndarray, np.ndarray]:
    if not transform:
        return np.eye(3), np.zeros(3)
    v = [float(x) for x in transform.split()]
    if len(v) != 12:
        return np.eye(3), np.zeros(3)
    return np.array(v[:9]).reshape(3, 3), np.array(v[9:])


def _cluster(verts: np.ndarray, tris: np.ndarray, cells: int) -> tuple[np.ndarray, np.ndarray]:
    """Vertex-clustering decimation: snap vertices to a ``cells``^3 grid, drop collapsed tris."""
    lo, hi = verts.min(0), verts.max(0)
    q = np.floor((verts - lo) / np.maximum(hi - lo, 1e-9).max() * cells).astype(np.int64)
    key = (q[:, 0] * (cells + 1) + q[:, 1]) * (cells + 1) + q[:, 2]
    uniq, inv = np.unique(key, return_inverse=True)
    cen = np.zeros((len(uniq), 3))
    cnt = np.bincount(inv, minlength=len(uniq)).astype(np.float64)
    for a in range(3):
        cen[:, a] = np.bincount(inv, weights=verts[:, a], minlength=len(uniq)) / cnt
    t = inv[tris]
    keep = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
    return cen, t[keep]


class _Source:
    """Lazy reader over one 3MF: parses each mesh file once."""

    def __init__(self, z: zipfile.ZipFile):
        self.z = z
        self._files: dict[str, dict[int, bytes]] = {}
        self._meshes: dict[tuple[str, int], Optional[tuple[np.ndarray, np.ndarray]]] = {}

    def mesh(self, path: Optional[str], obj_id: int, local: Optional[bytes] = None):
        key = (path or "", obj_id)
        if key in self._meshes:
            return self._meshes[key]
        if path:
            if path not in self._files:
                data = self.z.read(path.lstrip("/"))
                self._files[path] = {int(i): b for i, b in _OBJ_BLOCK.findall(data)}
            block = self._files[path].get(obj_id)
        else:
            block = local
        self._meshes[key] = _mesh_from_block(block) if block else None
        return self._meshes[key]


def _object_geometry(src: _Source, model_obj, res_by_id: dict, excluded_part_ids: set[int],
                     depth: int = 0) -> list[tuple[np.ndarray, np.ndarray]]:
    """[(verts, tris)] in the object's own frame, modifier parts excluded."""
    out: list[tuple[np.ndarray, np.ndarray]] = []
    if depth > 4:
        return out
    comps = model_obj.find(_q("components"))
    if comps is None:
        return out
    for comp in comps.findall(_q("component")):
        cid = int(comp.get("objectid"))
        if cid in excluded_part_ids:
            continue
        m, t = _mats(comp.get("transform"))
        path = comp.get(_p("path"))
        if path:
            geo = src.mesh(path, cid)
            parts = [geo] if geo else []
        else:
            sub = res_by_id.get(str(cid))
            parts = _object_geometry(src, sub, res_by_id, excluded_part_ids, depth + 1) if sub is not None else []
        for v, tr in parts:
            out.append((v @ m + t, tr))
    return out


# ------------------------------------------------------------------ rasteriser

def _render(parts: list[tuple[np.ndarray, np.ndarray]], size: int,
            rgb: tuple[int, int, int]) -> Optional[np.ndarray]:
    """Return an (size, size, 4) uint8 RGBA image, or None when there is nothing to draw."""
    if not parts:
        return None
    verts = np.concatenate([v for v, _ in parts])
    off, tris = 0, []
    for v, t in parts:
        tris.append(t + off)
        off += len(v)
    tris = np.concatenate(tris)
    ntri_raw = len(tris)
    if ntri_raw > _CLUSTER_ABOVE_TRIS:
        verts, tris = _cluster(verts, tris, cells=size * _SUPERSAMPLE)
    if len(tris) == 0:
        return None

    az, el = np.radians(-35.0), np.radians(32.0)
    ca, sa, ce, se = np.cos(az), np.sin(az), np.cos(el), np.sin(el)
    x = verts[:, 0] * ca - verts[:, 1] * sa
    y = verts[:, 0] * sa + verts[:, 1] * ca
    z = verts[:, 2]
    sx, sy, dp = x, z * ce + y * se, -y * ce + z * se
    W = size * _SUPERSAMPLE
    margin = 0.06 * W
    span = max(sx.max() - sx.min(), sy.max() - sy.min(), 1e-9)
    scale = (W - 2 * margin) / span
    px = (sx - sx.min()) * scale + margin + (W - 2 * margin - (sx.max() - sx.min()) * scale) / 2
    py = (sy.max() - sy) * scale + margin + (W - 2 * margin - (sy.max() - sy.min()) * scale) / 2
    P = np.stack([px, py, dp], 1)
    T = P[tris]                                   # (n, 3, 3)
    R = np.stack([x, y, z], 1)[tris]
    n = np.cross(R[:, 1] - R[:, 0], R[:, 2] - R[:, 0])
    ln = np.linalg.norm(n, axis=1)
    ok = ln > 1e-12
    T, n, ln = T[ok], n[ok], ln[ok]
    n = n / ln[:, None]
    view = np.array([0.0, -ce, se])
    flip = (n @ view) < 0
    n[flip] *= -1
    light = np.array([-0.55, -0.45, 0.70])
    light /= np.linalg.norm(light)
    shade = np.clip(0.30 + 0.45 * (n @ view) + 0.35 * np.clip(n @ light, 0, None), 0.0, 1.0)

    ext = np.maximum(T[:, :, :2].max(1) - T[:, :, :2].min(1), 0).max(1)
    k_tri = np.where(ext < 1.2, 1, np.minimum(np.ceil(ext / 0.8).astype(int), 60))

    # z-buffer by packed integer keys: (pixel << 32) | (quantised depth << 8) | shade8.
    # Sorting the keys puts the nearest sample last within each pixel.
    dlo, dhi = float(T[:, :, 2].min()), float(T[:, :, 2].max())
    dscale = (2 ** 23 - 1) / max(dhi - dlo, 1e-9)
    shade8 = np.clip(shade * 255, 0, 255).astype(np.int64)
    best = np.full(W * W, -1, dtype=np.int64)
    for k in np.unique(k_tri):
        sel = np.nonzero(k_tri == k)[0]
        if k == 1:
            B = np.array([[1 / 3, 1 / 3, 1 / 3]])
        else:
            B = np.array([(1 - (i + j) / k, i / k, j / k)
                          for i in range(k + 1) for j in range(k + 1 - i)])
        step = max(1, _MAX_CHUNK_SAMPLES // len(B))
        for s0 in range(0, len(sel), step):
            ids = sel[s0:s0 + step]
            pts = np.matmul(B, T[ids])                           # (m, nb, 3)
            ix = pts[:, :, 0].astype(np.int64).ravel()           # coords are >= 0 after margin
            iy = pts[:, :, 1].astype(np.int64).ravel()
            q = ((pts[:, :, 2].ravel() - dlo) * dscale).astype(np.int64)
            sh = np.repeat(shade8[ids], len(B))
            inside = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < W)
            if not inside.any():
                continue
            key = ((iy * W + ix)[inside] << 32) | (q[inside] << 8) | sh[inside]
            key.sort()
            pix = key >> 32
            last = np.nonzero(np.append(pix[1:] != pix[:-1], True))[0]
            u, kv = pix[last], key[last] & 0xFFFFFFFF
            win = (kv >> 8) > (best[u] >> 8)
            best[u[win]] = kv[win]

    zbuf = np.where(best >= 0, 0.0, -np.inf)
    shbuf = np.where(best >= 0, (best & 0xFF) / 255.0, 0.0)

    cover = np.isfinite(zbuf).reshape(W, W)
    if not cover.any():
        return None
    # close single-pixel pin-holes left by sampling (fill a pixel when >=5 of 8 neighbours are covered)
    pad = np.pad(cover, 1)
    nb = sum(pad[1 + dy:1 + dy + W, 1 + dx:1 + dx + W].astype(np.int8)
             for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dy) != (0, 0))
    holes = ~cover & (nb >= 5)
    sh2 = shbuf.reshape(W, W)
    if holes.any():
        padS = np.pad(sh2, 1)
        nsum = sum(padS[1 + dy:1 + dy + W, 1 + dx:1 + dx + W]
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dy) != (0, 0))
        sh2 = np.where(holes, nsum / np.maximum(nb, 1), sh2)
        cover = cover | holes

    col = np.array(rgb, dtype=np.float64)
    img = sh2[:, :, None] * col[None, None, :] * cover[:, :, None]
    a = cover.astype(np.float64)
    f = _SUPERSAMPLE
    img = img.reshape(size, f, size, f, 3).sum((1, 3))
    a_s = a.reshape(size, f, size, f).sum((1, 3))
    rgb_out = np.where(a_s[:, :, None] > 0, img / np.maximum(a_s[:, :, None], 1e-9), 0)
    out = np.dstack([np.clip(rgb_out, 0, 255), a_s / (f * f) * 255]).astype(np.uint8)
    return out


def _png(rgba: np.ndarray) -> bytes:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[r].tobytes() for r in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ public API

def _lift_dark(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black filament would render as a silhouette with no visible shading; lift it."""
    lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    if lum >= 70:
        return rgb
    t = (70 - lum) / 70
    return tuple(int(c + (150 - c) * t) for c in rgb)  # type: ignore[return-value]


def _hex_to_rgb(s: str) -> Optional[tuple[int, int, int]]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", (s or "").strip())
    if not m:
        return None
    v = m.group(1)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def object_previews(src_path: str, size: int = 256, with_images: bool = True) -> list[dict]:
    """One entry per object: id, name, extruder, plates it sits on, optional PNG preview.

    ``plates`` = [{"plate": n, "name": <plate name or "Plate n">, "instances": k}].
    ``preview`` = {"mime", "width", "height", "data_base64"} or None (nothing printable).
    """
    if not 32 <= size <= 1024:
        raise SubsetError("size must be between 32 and 1024")
    with zipfile.ZipFile(src_path) as z:
        names = z.namelist()
        model = _parse(z.read(MODEL))
        ms = _parse(z.read(MODEL_SETTINGS))
        colours: list[str] = []
        if PROJECT_SETTINGS in names:
            import json
            try:
                colours = json.loads(z.read(PROJECT_SETTINGS)).get("filament_colour") or []
            except ValueError:
                colours = []
        res_by_id = {o.get("id"): o for o in model.find(_q("resources")).findall(_q("object"))}

        plates: dict[str, list[dict]] = {}
        for n, pl in enumerate(ms.findall("plate"), start=1):
            pno = int(_meta(pl, "plater_id") or n)
            pname = (_meta(pl, "plater_name") or "").strip() or f"Plate {pno}"
            counts: dict[str, int] = {}
            for mi in pl.findall("model_instance"):
                oid = _meta(mi, "object_id")
                counts[oid] = counts.get(oid, 0) + 1
            for oid, k in counts.items():
                plates.setdefault(oid, []).append({"plate": pno, "name": pname, "instances": k})

        src = _Source(z)
        out = []
        for o in ms.findall("object"):
            oid = o.get("id")
            ext = _meta(o, "extruder")
            entry = {
                "id": int(oid),
                "name": _meta(o, "name") or f"object_{oid}",
                "extruder": ext,
                "plates": plates.get(oid, []),
                "preview": None,
            }
            if with_images and oid in res_by_id:
                excluded = {int(p.get("id")) for p in o.findall("part")
                            if p.get("subtype") in _NON_PRINTED_PARTS}
                rgb = _DEFAULT_RGB
                if ext and ext.isdigit() and 0 < int(ext) <= len(colours):
                    rgb = _lift_dark(_hex_to_rgb(colours[int(ext) - 1]) or rgb)
                img = _render(_object_geometry(src, res_by_id[oid], res_by_id, excluded), size, rgb)
                if img is not None:
                    entry["preview"] = {
                        "mime": "image/png", "width": size, "height": size,
                        "data_base64": base64.b64encode(_png(img)).decode("ascii"),
                    }
            out.append(entry)
    return out
