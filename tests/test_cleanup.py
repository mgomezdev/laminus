"""Tests for temp-directory cleanup failure tolerance (stale-job sweeper hardening)."""
from unittest.mock import patch

from app.main import cleanup_directory


def test_cleanup_directory_tolerates_rmtree_failure(tmp_path):
    """A failed rmtree (locked file, permission error, ...) must not raise —
    otherwise the caller (the stale-job sweep loop) dies silently and jobs/disk
    usage grow unbounded for the rest of the process lifetime."""
    target = tmp_path / "job_dir"
    target.mkdir()
    with patch("app.main.shutil.rmtree", side_effect=OSError("locked")):
        cleanup_directory(str(target))  # must not raise


def test_cleanup_directory_noop_on_missing_path(tmp_path):
    cleanup_directory(str(tmp_path / "does-not-exist"))  # must not raise
