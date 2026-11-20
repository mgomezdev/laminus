"""Tests for local desktop packages and diagnostics."""

import json
import os
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "orca-desktop.py"


def diagnostics(root, *args, check=True):
    return subprocess.run(
        [sys.executable, SCRIPT, *args], env={**os.environ, "ORCA_DESKTOP_ROOT": str(root)},
        capture_output=True, text=True, check=check,
    )


def test_seed_keeps_existing_package_and_settings(tmp_path):
    diagnostics(tmp_path, "seed")
    source = tmp_path / "packages" / "example.py"
    source.write_text("# first package\n")
    diagnostics(tmp_path, "seed")

    plugins = tmp_path / "state" / "orca_plugins"
    package = plugins / "example.py"
    sidecar = package / ".install_state.json"
    assert json.loads(sidecar.read_text()) == {"installed_from": "local", "enabled": True}
    assert "example.py\tenabled\texample.py" in diagnostics(tmp_path, "plugins").stdout

    sidecar.write_text('{"enabled": false, "permissions": {"network_http": ["https://example.test"]}}')
    source.write_text("# unrequested update\n")
    diagnostics(tmp_path, "seed")

    assert "example.py\tdisabled\texample.py" in diagnostics(tmp_path, "plugins").stdout
    assert (package / "example.py").read_text() == "# first package\n"
    assert json.loads(sidecar.read_text())["permissions"]["network_http"] == ["https://example.test"]


def test_seed_rejects_ambiguous_packages(tmp_path):
    inbox = tmp_path / "packages"
    inbox.mkdir()
    (inbox / "example.py").touch()
    (inbox / "example.whl").touch()

    result = diagnostics(tmp_path, "seed", check=False)
    assert result.returncode == 1
    assert "Multiple local packages use the name example" in result.stderr
    assert not list((tmp_path / "state" / "orca_plugins").iterdir())


def test_logs_match_case_insensitively(tmp_path):
    logs = tmp_path / "state" / "log"
    logs.mkdir(parents=True)
    (logs / "debug_01.log.0").write_text("SpoolMan loaded\nUnrelated warning\n")
    assert diagnostics(tmp_path, "logs", "--match", "spoolman").stdout == (
        "debug_01.log.0: SpoolMan loaded\n"
    )
