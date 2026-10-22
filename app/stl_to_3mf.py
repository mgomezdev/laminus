"""Convert a bare STL (binary or ASCII) into a minimal valid 3MF."""
from __future__ import annotations

import os
import re
import struct
import zipfile

_NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_NS_BAMBU = "http://schemas.bambulab.com/package/2021"
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_REL_TYPE_3D = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"

_OBJ_MODEL_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<model unit="millimeter" xml:lang="en-US"'
    f' xmlns="{_NS_CORE}"'
    f' xmlns:BambuStudio="{_NS_BAMBU}"'
    f' xmlns:p="{_NS_PROD}" requiredextensions="p">\n'
    f' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
)

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


def _mesh_body(tris: list[tuple], indent: str) -> tuple[str, str]:
    """Return (vertices_block, triangles_block) as newline-joined lines at *indent*."""
    vlines, tlines = [], []
    for i, tri in enumerate(tris):
        for x, y, z in tri:
            vlines.append(f'{indent}<vertex x="{x}" y="{y}" z="{z}"/>')
        base = i * 3
        tlines.append(f'{indent}<triangle v1="{base}" v2="{base+1}" v3="{base+2}"/>')
    return "\n".join(vlines), "\n".join(tlines)


def _build_model_xml(tris: list[tuple]) -> str:
    verts, tris_xml = _mesh_body(tris, "          ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        '  <resources><object id="1" type="model"><mesh>\n'
        f'    <vertices>\n{verts}\n    </vertices>\n'
        f'    <triangles>\n{tris_xml}\n    </triangles>\n'
        '  </mesh></object></resources>\n'
        '  <build><item objectid="1"/></build>\n</model>'
    )


def stl_to_3mf(stl_path: str, out_path: str) -> None:
    """Convert stl_path to a minimal 3MF written at out_path."""
    tris = _parse_binary(stl_path) if _is_binary(stl_path) else _parse_ascii(stl_path)
    if not tris:
        raise ValueError(f"No triangles found in '{os.path.basename(stl_path)}' (empty or unparseable STL)")
    model_xml = _build_model_xml(tris)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("3D/3dmodel.model", model_xml.encode("utf-8"))


def parse_stl_triangles(path: str) -> list[tuple]:
    """Return [(v0,v1,v2), ...] triangles from an STL file (binary or ASCII)."""
    return _parse_binary(path) if _is_binary(path) else _parse_ascii(path)


def _object_model_xml(tris: list[tuple], object_id: int = 1) -> str:
    """Build a 3D/Objects/*.model XML string for a single mesh."""
    verts, tris_xml = _mesh_body(tris, "     ")
    return (
        _OBJ_MODEL_HEADER
        + ' <resources>\n'
        + f'  <object id="{object_id}" type="model">\n'
          '   <mesh>\n'
          '    <vertices>\n'
        + verts
        + '\n    </vertices>\n'
          '    <triangles>\n'
        + tris_xml
        + '\n    </triangles>\n'
          '   </mesh>\n'
          '  </object>\n'
          ' </resources>\n'
          '</model>'
    )


def inject_stls_into_3mf(
    template_path: str,
    stl_paths: list[str],
    out_path: str,
    quantities: list[int] | None = None,
) -> None:
    """Build a 3MF that carries settings from *template_path* and geometry from *stl_paths*.

    The template's Metadata/project_settings.config (with all printer fields) is preserved
    verbatim. All geometry in the template is discarded and replaced with fresh objects
    built from the supplied STL files. The result is ready for OrcaSlicer --arrange.

    *quantities* (aligned with *stl_paths*, default 1 each) yields that many build items per
    STL; all copies of one STL reference a single shared mesh file.

    Raises ValueError if the template lacks Metadata/project_settings.config, or if
    *quantities* does not match *stl_paths* in length.
    """
    if quantities is None:
        quantities = [1] * len(stl_paths)
    if len(quantities) != len(stl_paths):
        raise ValueError("quantities must have the same length as stl_paths")

    with zipfile.ZipFile(template_path, "r") as src:
        names = src.namelist()
        if "Metadata/project_settings.config" not in names:
            raise ValueError("Template 3MF is missing Metadata/project_settings.config")
        project_settings = src.read("Metadata/project_settings.config")

    object_files: dict[str, bytes] = {}
    resources_parts: list[str] = []
    build_parts: list[str] = []
    rels_parts: list[str] = []

    wrapper_id = 0
    for i, (stl_path, qty) in enumerate(zip(stl_paths, quantities), start=1):
        stem = os.path.splitext(os.path.basename(stl_path))[0]
        obj_path = f"3D/Objects/{stem}_{i}.model"

        tris = parse_stl_triangles(stl_path)
        if not tris:
            raise ValueError(f"No triangles found in '{os.path.basename(stl_path)}' (empty or unparseable STL)")
        object_files[obj_path] = _object_model_xml(tris, i).encode("utf-8")

        for _ in range(qty):
            wrapper_id += 2  # 2, 4, 6, … — matches OrcaSlicer component/wrapper pattern
            resources_parts.append(
                f'  <object id="{wrapper_id}" type="model">\n'
                f'   <components>\n'
                f'    <component p:path="/{obj_path}" objectid="{i}"'
                f' transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
                f'   </components>\n'
                f'  </object>'
            )
            build_parts.append(f'  <item objectid="{wrapper_id}" printable="1"/>')
        rels_parts.append(
            f' <Relationship Target="/{obj_path}" Id="rel-{i}"'
            f' Type="{_REL_TYPE_3D}"/>'
        )

    main_model = (
        _OBJ_MODEL_HEADER
        + ' <resources>\n'
        + '\n'.join(resources_parts)
        + '\n </resources>\n'
          ' <build>\n'
        + '\n'.join(build_parts)
        + '\n </build>\n'
          '</model>'
    )

    model_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Relationships xmlns="{_NS_REL}">\n'
        + '\n'.join(rels_parts)
        + '\n</Relationships>'
    )

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        dst.writestr("[Content_Types].xml", _CONTENT_TYPES)
        dst.writestr("_rels/.rels", _RELS)
        dst.writestr("Metadata/project_settings.config", project_settings)
        dst.writestr("3D/3dmodel.model", main_model.encode("utf-8"))
        dst.writestr("3D/_rels/3dmodel.model.rels", model_rels.encode("utf-8"))
        for path, content in object_files.items():
            dst.writestr(path, content)


# OrcaSlicer 2.3.x/2.4.x on Linux segfaults during --slice when certain metadata
# values in .model files contain version strings.  Two known triggers:
#   - <metadata name="Application">BambuStudio-2.3.2</metadata>  (dash-version suffix)
#   - <metadata name="OrcaSlicer">2.4.1</metadata>               (bare version, v2.4.1+)
# Strip both by removing those metadata elements entirely; they are informational only.
_VERSION_META_RE = re.compile(
    r'[ \t]*<metadata\s+name="(?:Application|OrcaSlicer)"\s*>[^<]*</metadata>\r?\n?',
    re.IGNORECASE,
)


def strip_application_version(src_path: str, dst_path: str) -> None:
    """Copy *src_path* to *dst_path* with two fixes for OrcaSlicer Linux slice bugs:

    1. Remove version-bearing metadata tags from .model files to avoid segfaults
       (triggered by Application/OrcaSlicer metadata in both v2.3.x and v2.4.x).

    2. If project_settings.config has ``use_relative_e_distances = "1"`` but an
       empty ``layer_gcode``, override to ``"0"`` so OrcaSlicer generates M82 +
       absolute E values — correct for machines whose start-gcode uses M83 only
       for purge lines before handing off to the slicer's generated print body.
    """
    import json as _json

    with zipfile.ZipFile(src_path, "r") as src:
        with zipfile.ZipFile(dst_path, "w", compression=zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename.endswith(".model"):
                    text = data.decode("utf-8", errors="replace")
                    text = _VERSION_META_RE.sub("", text)
                    data = text.encode("utf-8")
                elif item.filename == "Metadata/project_settings.config":
                    try:
                        cfg = _json.loads(data.decode("utf-8"))
                        if cfg.get("use_relative_e_distances") == "1":
                            lg = cfg.get("layer_gcode", "")
                            if not lg or "G92" not in str(lg):
                                cfg["use_relative_e_distances"] = "0"
                                data = _json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
                    except Exception:
                        pass
                dst.writestr(item, data)


def stl_min_z(stl_path: str) -> float:
    """Lowest Z coordinate of an STL's vertices (0.0 if it has no triangles)."""
    tris = parse_stl_triangles(stl_path)
    return min((v[2] for tri in tris for v in tri), default=0.0)


_BUILD_ITEM_RE = re.compile(r'(<item\b[^>]*?\btransform=")([^"]*)(")')


def restore_build_item_z(packed_path: str, z_offsets: list[float]) -> None:
    """Add z_offsets[k] to the Z translation of the build item for input instance k.

    OrcaSlicer re-seats every object at min Z = 0 on load; call this to put each
    instance back at its input STL's min Z. Valid only when no X/Y rotation was applied.
    Orca reorders and renumbers objects, so instance k is found via the
    `source_object_id` it records in Metadata/model_settings.config (the input position).
    Rewrites *packed_path* in place; raises ValueError if the 3MF doesn't match z_offsets.
    """
    with zipfile.ZipFile(packed_path, "r") as src:
        entries = [(info, src.read(info.filename)) for info in src.infolist()]
    files = {info.filename: data for info, data in entries}

    cfg = files.get("Metadata/model_settings.config", b"").decode("utf-8")
    source_of: dict[str, int] = {}
    for obj in re.finditer(r'<object id="(\d+)">(.*?)</object>', cfg, re.S):
        m = re.search(r'key="source_object_id" value="(\d+)"', obj.group(2))
        if m:
            source_of[obj.group(1)] = int(m.group(1))

    text = files["3D/3dmodel.model"].decode("utf-8")
    items = list(_BUILD_ITEM_RE.finditer(text))
    if len(items) != len(z_offsets):
        raise ValueError(f"packed 3MF has {len(items)} build items, expected {len(z_offsets)}")

    def _shift(m: re.Match) -> str:
        oid = re.search(r'objectid="(\d+)"', m.group(0)).group(1)
        k = source_of.get(oid)
        if k is None or not 0 <= k < len(z_offsets):
            raise ValueError(f"build item {oid} has no valid source_object_id")
        vals = m.group(2).split()
        vals[11] = repr(float(vals[11]) + z_offsets[k])
        return m.group(1) + " ".join(vals) + m.group(3)

    files["3D/3dmodel.model"] = _BUILD_ITEM_RE.sub(_shift, text).encode("utf-8")

    with zipfile.ZipFile(packed_path, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for info, _ in entries:
            dst.writestr(info.filename, files[info.filename])
