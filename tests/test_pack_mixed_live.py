"""Live /api/v1/pack/mixed check against a running Laminus (real OrcaSlicer arrange + slice).

BIZ-356 acceptance: STL x3 + 3MF object x2 + 3MF plate bundle (together) x2 + solo STL x2 on
a 256 mm bed gives a multi-plate 3MF whose manifest sums to the request, together copies whole
and solo instances alone, and every plate slices with POST /api/v1/jobs plate=N.

Skipped unless LAMINUS_URL is set (see test_pack_quantities_live.py).
"""
import json
import time

import httpx
import pytest

from tests.test_export_3mf_live import BASE, PRINTERS
from tests.test_pack_mixed import _check
from tests.test_pack_next import _box_stl

pytestmark = pytest.mark.skipif(not BASE, reason="LAMINUS_URL not set (needs a live Laminus + OrcaSlicer)")


def _orca_3mf():
    """A real OrcaSlicer project (the synthetic test 3MFs do not load in Orca): /pack of two
    70 x 60 plates and a 30 mm cube, all on plate 1."""
    stls = [("p1.stl", (70, 60, 10)), ("p2.stl", (70, 60, 10)), ("cube.stl", (30, 30, 30))]
    r = httpx.post(f"{BASE}/api/v1/pack", timeout=300,
                   files=[("files", (n, _box_stl(*d), "application/octet-stream")) for n, d in stls],
                   data={"bed_x": "256", "bed_y": "256", "bed_z": "250"})
    assert r.status_code == 200, r.text
    return r.content


# STL x3, 3MF object x2, plate 1 bundle (3 objects, together) x2, solo STL x2
SEL = [{"file": "b.stl", "qty": 3},
       {"file": "a.3mf", "id": 6, "qty": 2},
       {"file": "a.3mf", "plate": 1, "qty": 2, "rule": "together"},
       {"file": "c.stl", "qty": 2, "rule": "solo"}]
REQUESTED = (3, 2, 6, 2)


def _files(tmp_path):
    return [("files", ("a.3mf", _orca_3mf(), "application/octet-stream")),
            ("files", ("b.stl", _box_stl(40, 40, 20), "application/octet-stream")),
            ("files", ("c.stl", _box_stl(50, 50, 50), "application/octet-stream"))]


class _R:
    """httpx response shaped like a TestClient one for _check."""

    def __init__(self, r):
        self.status_code, self.content, self.headers, self.text = r.status_code, r.content, r.headers, r.text


def _pack(tmp_path):
    r = httpx.post(f"{BASE}/api/v1/pack/mixed", files=_files(tmp_path), timeout=600,
                   data={"selection": json.dumps(SEL), "bed_x": "256", "bed_y": "256", "bed_z": "250"})
    m = _check(_R(r), tmp_path, 256, REQUESTED)
    assert "floating" not in r.headers["X-Merge-Warnings"] and "below" not in r.headers["X-Merge-Warnings"]
    return r, m


def test_acceptance_selection_packs(tmp_path):
    _pack(tmp_path)


def test_acceptance_every_plate_slices(tmp_path):
    r, m = _pack(tmp_path)
    machine, process, filament, _ = PRINTERS["bambu_p1s"]          # 256 x 256 bed
    ids = []
    for pl in m["plates"]:
        j = httpx.post(f"{BASE}/api/v1/jobs", timeout=60,
                       files={"file": ("mixed.3mf", r.content, "application/octet-stream")},
                       data={"machine_uuid": machine, "process_uuid": process,
                             "filament_uuids": json.dumps([filament]), "plate": str(pl["plate"])})
        assert j.status_code == 202, j.text
        ids.append(j.json()["id"])
    deadline = time.monotonic() + 600
    for job_id in ids:
        while True:
            job = httpx.get(f"{BASE}/api/v1/jobs/{job_id}", timeout=30).json()
            if job["status"] in ("completed", "failed") or time.monotonic() > deadline:
                break
            time.sleep(2)
        assert job["status"] == "completed", job


def test_part_too_large_live(tmp_path):
    r = httpx.post(f"{BASE}/api/v1/pack/mixed", files=_files(tmp_path), timeout=600,
                   data={"selection": json.dumps(SEL), "bed_x": "45", "bed_y": "45", "bed_z": "250"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "part_too_large"
