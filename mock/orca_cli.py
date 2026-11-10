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
import re
import sys
import time
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


def _input_object_ids(input_path):
    """Object ids of the input project's build items, in order ([] when it has no readable model)."""
    try:
        with zipfile.ZipFile(input_path) as zf:
            model = zf.read("3D/3dmodel.model").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile, TypeError):
        return []
    return re.findall(r'<item\b[^>]*\bobjectid="([^"]+)"', model)


def _write_arranged_3mf(path, input_path):
    """What `--arrange --export-3mf` leaves behind, as far as Laminus reads it: the input's model, and a
    `Metadata/model_settings.config` naming each object's input position as its part's `source_object_id`,
    all on one plate. Laminus's pack manifest (app/manifest.py `pack_origin`) is built from that file."""
    ids = _input_object_ids(input_path)
    if not ids:
        _write_3mf(path)
        return
    with zipfile.ZipFile(input_path) as zf:
        model = zf.read("3D/3dmodel.model")
    settings = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>'
                + "".join(f'<object id="{oid}"><metadata key="name" value="obj{k}"/>'
                          f'<part id="1"><metadata key="source_object_id" value="{k}"/></part></object>'
                          for k, oid in enumerate(ids))
                + '<plate><metadata key="plater_id" value="1"/>'
                + "".join(f'<model_instance><metadata key="object_id" value="{oid}"/>'
                          f'<metadata key="instance_id" value="0"/></model_instance>' for oid in ids)
                + "</plate></config>")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("Metadata/project_settings.config", _PROJECT_SETTINGS)
        zf.writestr("3D/3dmodel.model", model)
        zf.writestr("Metadata/model_settings.config", settings)


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
        # e2e hooks, keyed on the plate the client requested (the plate is the only knob a
        # client controls): slicing MOCK_ORCA_SLOW_PLATE takes MOCK_ORCA_SLICE_DELAY
        # seconds, slicing MOCK_ORCA_FAIL_PLATE fails. Laminus extracts the requested plate
        # into "<input>_plate<N>.3mf" and always slices it as plate 1, so read N from there.
        m = re.search(r"_plate(\d+)\.3mf$", args[-1]) if args else None
        if m:
            plate = int(m.group(1))
        delay = float(os.environ.get("MOCK_ORCA_SLICE_DELAY", "0"))
        if delay and os.environ.get("MOCK_ORCA_SLOW_PLATE") == str(plate):
            for step in range(1, 4):
                print(f"mock: slicing step {step}/3", flush=True)
                time.sleep(delay / 3)
        if os.environ.get("MOCK_ORCA_FAIL_PLATE") == str(plate):
            print("mock: ERROR slicing failed: mesh is not manifold", flush=True)
            sys.exit(1)
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
        _write_arranged_3mf(export_3mf, args[-1] if args else None)

    sys.exit(0)


if __name__ == "__main__":
    main()
