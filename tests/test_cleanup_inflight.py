"""Regression: POST /api/cleanup (via _scan_stale_temp) must not sweep up ARRANGE_DIR
scratch dirs or stable output copies belonging to an in-flight /api/pack, /api/arrange,
or /api/slice/thumbnail request, even with min_age_seconds=0 (remove-everything mode).
"""
import os

import app.main as main_mod
from app.main import _scan_stale_temp, _track_inflight_arrange


def test_scan_stale_temp_excludes_inflight_arrange_dirs(tmp_path, monkeypatch):
    arrange_dir = tmp_path / "arrange"
    arrange_dir.mkdir()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(main_mod, "ARRANGE_DIR", str(arrange_dir))
    monkeypatch.setattr(main_mod, "JOBS_DIR", str(jobs_dir))

    job_id = "in-flight-job-1"
    inflight_scratch = arrange_dir / f"pack_{job_id}"
    inflight_scratch.mkdir()
    (inflight_scratch / "input.stl").write_bytes(b"x")
    inflight_stable_out = arrange_dir / f"{job_id}_packed.3mf"
    inflight_stable_out.write_bytes(b"x")

    orphaned = arrange_dir / "pack_orphaned-job"
    orphaned.mkdir()

    with _track_inflight_arrange(job_id):
        # min_age_seconds=0 - "remove everything unowned regardless of age".
        stale = _scan_stale_temp(min_age_seconds=0)

    stale_names = {os.path.basename(p) for p in stale}
    assert f"pack_{job_id}" not in stale_names, "in-flight scratch dir must not be swept"
    assert f"{job_id}_packed.3mf" not in stale_names, "in-flight stable output must not be swept"
    assert "pack_orphaned-job" in stale_names, "genuinely orphaned dir should still be swept"


def test_scan_stale_temp_sweeps_after_inflight_released(tmp_path, monkeypatch):
    arrange_dir = tmp_path / "arrange"
    arrange_dir.mkdir()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(main_mod, "ARRANGE_DIR", str(arrange_dir))
    monkeypatch.setattr(main_mod, "JOBS_DIR", str(jobs_dir))

    job_id = "in-flight-job-2"
    scratch = arrange_dir / f"pack_{job_id}"
    scratch.mkdir()

    with _track_inflight_arrange(job_id):
        pass  # request finished; job_id released on exit

    stale = _scan_stale_temp(min_age_seconds=0)
    stale_names = {os.path.basename(p) for p in stale}
    assert f"pack_{job_id}" in stale_names
