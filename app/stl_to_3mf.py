"""Convert a bare STL (binary or ASCII) into a minimal valid 3MF."""
from __future__ import annotations

import os
import struct
import zipfile

_CONTENT_TYPES = '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'

_RELS = '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'


def _is_binary(path: str) -> bool:
    with open(path, "rb") as f:
        header = f.read(80)
        if not header.startswith(b"solid"):
            return True
        count_bytes = f.read(4)
        if len(count_bytes) < 4:
            return False
        count = struct.unpack("<I", count_bytes)[0]
    expected_size = 80 + 4 + count * 50
    actual_size = os.path.getsize(path)
    return actual_size == expected_size


def _parse_binary(path: str) -> list[tuple]:
    tris = []
    with open(path, "rb") as f:
        f.read(80)
        count = struct.unpack("<I", f.read(4))[0]
        for _ in range(count):
            f.read(12)
            verts = [struct.unpack("<fff", f.read(12)) for _ in range(3)]
            f.read(2)
            tris.append(tuple(verts))
    return tris


def _parse_ascii(path: str) -> list[tuple]:
    tris, verts = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("vertex"):
                parts = line.split()
                if len(parts) >= 4:
                    verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
                    if len(verts) == 3:
                        tris.append(tuple(verts))
                        verts = []
    return tris


def _build_model_xml(tris: list[tuple]) -> str:
    vlines, tlines = [], []
    idx = 0
    for tri in tris:
        for x, y, z in tri:
            vlines.append(f'          <vertex x="{x}" y="{y}" z="{z}"/>')
        tlines.append(f'          <triangle v1="{idx}" v2="{idx+1}" v3="{idx+2}"/>')
        idx += 3
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        '  <resources><object id="1" type="model"><mesh>\n'
        f'    <vertices>\n{chr(10).join(vlines)}\n    </vertices>\n'
        f'    <triangles>\n{chr(10).join(tlines)}\n    </triangles>\n'
        '  </mesh></object></resources>\n'
        '  <build><item objectid="1"/></build>\n</model>'
    )


def stl_to_3mf(stl_path: str, out_path: str) -> None:
    """Convert stl_path to a minimal 3MF written at out_path."""
    tris = _parse_binary(stl_path) if _is_binary(stl_path) else _parse_ascii(stl_path)
    model_xml = _build_model_xml(tris)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("3D/3dmodel.model", model_xml.encode("utf-8"))
