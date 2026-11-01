"""Live /api/v1/jobs export_3mf check against a running Laminus (real OrcaSlicer).

BIZ-351: with a Laminus-built project_settings.config, `--slice 1 --export-3mf` wrote
plate_1.gcode and then segfaulted, so no .gcode.3mf was produced. The job must complete and
its artifact must carry the sliced plate's G-code.

Skipped unless LAMINUS_URL is set (see test_pack_quantities_live.py).
"""
import io
import json
import os
import struct
import time
import zipfile

import httpx
import pytest

BASE = os.environ.get("LAMINUS_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not BASE, reason="LAMINUS_URL not set (needs a live Laminus + OrcaSlicer)")

_CUBE_TRIS = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
              (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]

# Catalog UUIDs are uuid5(source, rel_path) of the stock vendor presets, so they are stable.
# BIZ-350: none of these need extra_config to get past Orca's -51 per-layer G92 E0 check.
PRINTERS = {
    "ender3v3se": ("7de6d4b3-bb28-54d3-a43b-e00fce0b406b", "9fdd9e18-eb67-5940-8fb8-c1b61e6f8ddd",
                   "92c6d75e-9e9d-5eca-8e3f-aa65e54e2add", None),
    "centauri_carbon": ("8aaac37c-14a7-5a53-b9f7-3dd1df210919", "55ec8331-c222-59f2-8338-e1a76f04b39f",
                        "9c15730f-c907-5ad0-b7dd-3519fd3bde57", None),
    "bambu_p1s": ("0f0a771f-0a29-5077-8571-e1fee605f432", "1d346219-eebe-5de8-af9a-eff4c7a8e496",
                  "71f6417e-2b5c-5222-b7a9-534cf4836af6", None),
    "snapmaker_u1": ("17a42182-583e-542a-ad7b-7ef58b2c1e77", "08a9b745-deeb-5b00-84d0-3b7ef57ba2e8",
                     "bcab178e-4ca7-510f-a0ed-97837d9d3c48", None),
}


def _cube_stl(size=20.0):
    verts = [(a, b, c) for a in (0, size) for b in (0, size) for c in (0, size)]
    buf = io.BytesIO()
    buf.write(b"\0" * 80)
    buf.write(struct.pack("<I", len(_CUBE_TRIS)))
    for tri in _CUBE_TRIS:
        buf.write(struct.pack("<3f", 0, 0, 0))
        for i in tri:
            buf.write(struct.pack("<3f", *verts[i]))
        buf.write(b"\0\0")
    return buf.getvalue()


def _assert_exported(job_id):
    deadline = time.monotonic() + 300
    while True:
        job = httpx.get(f"{BASE}/api/v1/jobs/{job_id}", timeout=30).json()
        if job["status"] in ("completed", "failed") or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert job["status"] == "completed", job

    art = httpx.get(f"{BASE}/api/v1/jobs/{job_id}/artifact", timeout=60)
    assert art.status_code == 200
    with zipfile.ZipFile(io.BytesIO(art.content)) as z:
        gcode = z.read("Metadata/plate_1.gcode")
    assert b"G1 " in gcode


@pytest.mark.parametrize("printer", sorted(PRINTERS))
def test_jobs_export_3mf_produces_sliced_3mf(printer):
    machine, process, filament, extra = PRINTERS[printer]
    data = {"machine_uuid": machine, "process_uuid": process,
            "filament_uuids": json.dumps([filament]), "plate": "1", "export_3mf": "out.gcode.3mf"}
    if extra:
        data["extra_config"] = json.dumps(extra)
    r = httpx.post(f"{BASE}/api/v1/jobs", data=data,
                   files={"file": ("cube.stl", _cube_stl(), "application/octet-stream")}, timeout=60)
    assert r.status_code == 202, r.text
    _assert_exported(r.json()["id"])


@pytest.mark.parametrize("printer, flavor", [("bambu_p1s", "marlin"), ("centauri_carbon", "klipper"),
                                             ("snapmaker_u1", "klipper")])
def test_presets_resolve_against_their_own_vendor(printer, flavor):
    # BIZ-350: shared base names (fdm_machine_common, ...) resolved to another vendor's
    # file - P1S got Wanhao's base and sliced as klipper.
    machine, process, filament, _ = PRINTERS[printer]
    r = httpx.post(f"{BASE}/api/v1/profiles/merged-config", timeout=60,
                   json={"machine_uuid": machine, "process_uuid": process, "filament_uuids": [filament]})
    assert r.status_code == 200, r.text
    assert r.json()["gcode_flavor"] == flavor


def test_prepared_export_3mf_repairs_filament_ids():
    # Packs made before the fix carry `filament_ids: []` (and some a stray `filament_id`).
    machine, process, filament, _ = PRINTERS["centauri_carbon"]
    r = httpx.post(f"{BASE}/api/v1/pack", data={"machine_uuid": machine, "process_uuid": process,
                                               "filament_uuids": json.dumps([filament])},
                   files=[("files", ("cube.stl", _cube_stl(), "application/octet-stream"))], timeout=300)
    assert r.status_code == 200, r.text
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(r.content)) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "Metadata/project_settings.config":
                cfg = json.loads(data)
                cfg["filament_ids"], cfg["filament_id"] = [], ["GFL99"]
                data = json.dumps(cfg)
            dst.writestr(item, data)
    r = httpx.post(f"{BASE}/api/v1/slice/prepared", data={"plate": "1", "export_3mf": "out.gcode.3mf"},
                   files={"file": ("pack.3mf", buf.getvalue(), "application/octet-stream")}, timeout=60)
    assert r.status_code == 200, r.text
    _assert_exported(r.json()["job_id"])
