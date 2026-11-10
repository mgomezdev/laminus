"""Tests for temp-directory cleanup failure tolerance (stale-job sweeper hardening)."""
import logging
from unittest.mock import patch

from app.main import cleanup_directory


def test_cleanup_directory_removes_the_folder(tmp_path):
    target = tmp_path / "job_dir"
    (target / "input").mkdir(parents=True)
    (target / "input" / "m.stl").write_bytes(b"x")
    cleanup_directory(str(target))
    assert not target.exists()


def test_cleanup_directory_tolerates_rmtree_failure(tmp_path, caplog):
    """A failed rmtree (locked file, permission error, ...) must not raise —
    otherwise the caller (the stale-job sweep loop) dies silently and jobs/disk
    usage grow unbounded for the rest of the process lifetime."""
    target = tmp_path / "job_dir"
    target.mkdir()
    with caplog.at_level(logging.WARNING), patch("app.main.shutil.rmtree", side_effect=OSError("locked")) as rmtree:
        cleanup_directory(str(target))  # must not raise
    rmtree.assert_called_once_with(str(target))
    assert target.exists()                                          # nothing was removed...
    assert f"Failed to clean up temp folder: {target}" in caplog.text    # ...and the failure is visible in the log


def test_cleanup_directory_noop_on_missing_path(tmp_path, caplog):
    with caplog.at_level(logging.INFO), patch("app.main.shutil.rmtree") as rmtree:
        cleanup_directory(str(tmp_path / "does-not-exist"))  # must not raise
    rmtree.assert_not_called()
    assert caplog.text == ""                                        # nothing to do is not worth a log line
