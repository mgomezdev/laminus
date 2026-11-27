"""Exercise the plugin host using the real OrcaSlicer release in Docker."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("LAMINUS_TEST_ORCA_DESKTOP") != "1",
    reason="set LAMINUS_TEST_ORCA_DESKTOP=1 to download and run OrcaSlicer in Docker",
)
REPO = Path(__file__).resolve().parents[1]
SERVICE = "orca-desktop"
PLUGIN_SUCCESS = "[load_plugin_impl] SUCCESS plugin=laminus_ci"


@pytest.fixture
def desktop(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    shutil.copyfile(REPO / "tests/fixtures/orca_plugins/laminus_ci.py", packages / "laminus_ci.py")
    project = f"laminus-desktop-test-{uuid.uuid4().hex[:8]}"
    env = {
        **os.environ, "ORCA_DESKTOP_CONFIG_DIR": str(tmp_path),
        "ORCA_DESKTOP_VERSION": "2.5.1", "ORCA_DESKTOP_VNC": "1",
        "ORCA_DESKTOP_VNC_PORT": "0",
    }

    def compose(*args, check=True):
        result = subprocess.run(
            ["docker", "compose", "-p", project, "--profile", "desktop", *args],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=360,
        )
        if check and result.returncode:
            pytest.fail(f"docker compose {' '.join(args)} exited {result.returncode}\n"
                        f"{result.stdout}\n{result.stderr}")
        return result.stdout

    try:
        compose("build", SERVICE)
        compose("run", "--rm", "--no-deps", "--entrypoint", "x11vnc", SERVICE,
                "-storepasswd", "ci-only", "/config/orca-desktop/vnc.pass")
        yield compose, tmp_path
    finally:
        print(compose("logs", "--no-color", SERVICE, check=False))
        compose("down", "--volumes", check=False)


def wait_for_load(compose, state, runs):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if SERVICE in compose("ps", "--status", "exited", "--services").splitlines():
            pytest.fail(f"{SERVICE} exited before loading the plugin")
        logs = compose("exec", "-T", SERVICE, "orca-diagnostics", "logs",
                       "--match", PLUGIN_SUCCESS, check=False)
        if logs.count(PLUGIN_SUCCESS) >= runs:
            config = state / "config.json"
            if config.is_file():
                settings = json.loads(config.read_text())["config"]
                assert [entry["cap_config"] for entry in settings] == [{"runs": runs}]
                return
        time.sleep(2)
    logs = compose("exec", "-T", SERVICE, "orca-diagnostics", "logs", "--match", "plugin",
                   check=False)
    pytest.fail(f"Orca did not load the plugin for run {runs}\n{logs[-8000:]}")


def test_orca_loads_plugin_and_keeps_settings_on_recreation(desktop):
    compose, root = desktop
    state = root / "state" / "orca_plugins"
    compose("up", "-d", SERVICE)
    wait_for_load(compose, state, runs=1)
    version = compose("exec", "-T", SERVICE, "cat", "/opt/orcaslicer/.version")
    assert version.strip() == "2.5.1"
    assert "laminus_ci.py\tenabled\tlaminus_ci.py" in compose(
        "exec", "-T", SERVICE, "orca-diagnostics", "plugins"
    )

    host, _, port = compose("port", SERVICE, "5900").strip().rpartition(":")
    assert host == "127.0.0.1"
    with socket.create_connection((host, int(port)), timeout=10) as vnc, vnc.makefile("rb") as stream:
        banner = stream.readline()
        assert banner.startswith(b"RFB 003.")
        vnc.sendall(banner)
        assert stream.read(2) == b"\x01\x02"  # VNC Authentication is the only offered method.

    (root / "packages" / "laminus_ci.py").unlink()
    compose("restart", SERVICE)
    wait_for_load(compose, state, runs=2)
    compose("up", "-d", "--force-recreate", SERVICE)
    wait_for_load(compose, state, runs=3)
