#!/usr/bin/env python3
"""Mock OrcaSlicer CLI for CI testing.

Handles the subset of OrcaSlicer's CLI that Laminus invokes so CI can run
server tests without downloading the real 200 MB AppImage.

Supported invocations:
  orcaslicer --version
  orcaslicer --slice N --outputdir DIR --arrange N [--export-3mf NAME] INPUT
  orcaslicer --datadir DIR --export-3mf PATH [--arrange N] [--orient N] INPUT
"""
import os
import sys
import zipfile

# Minimal 1×1 red PNG (37 bytes)
_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

_CONTENT_TYPES = (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)
_PROJECT_SETTINGS = '{"from":"user","layer_height":"0.2","printable_area":["0x0","200x0","200x200","0x200"]}'


def _get(args, flag):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _write_3mf(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("Metadata/project_settings.config", _PROJECT_SETTINGS)
        zf.writestr("3D/3dmodel.model", "<model/>")


def _write_thumbnail_3mf(path, plate):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr(f"Metadata/plate_{plate}.png", _PNG_1x1)


def main():
    args = sys.argv[1:]

    if "--version" in args:
        print("OrcaSlicer-2.4.0-mock")
        sys.exit(0)

    output_dir = _get(args, "--outputdir")
    export_3mf = _get(args, "--export-3mf")

    if "--slice" in args:
        plate = int(_get(args, "--slice") or 1)
        if output_dir and export_3mf:
            # Slice → 3MF: outputdir/export_3mf (relative name)
            _write_3mf(os.path.join(output_dir, export_3mf))
        elif export_3mf:
            # Thumbnail: absolute path, include plate PNG
            _write_thumbnail_3mf(export_3mf, plate)
        elif output_dir:
            # Plain gcode output
            with open(os.path.join(output_dir, "plate_1.gcode"), "w") as f:
                f.write("; OrcaSlicer mock\nG28\nM84\n")
    elif export_3mf:
        # Pack / arrange: absolute path
        _write_3mf(export_3mf)

    sys.exit(0)


if __name__ == "__main__":
    main()
