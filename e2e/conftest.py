"""E2E fixtures: boot the real Laminus app against the mock OrcaSlicer CLI.

Each test gets its own uvicorn process with fresh CONFIG/DATA/JOBS dirs, so job
history and the queue start empty. Linux-only (the app needs POSIX process groups);
run via WSL or the CI `e2e` job.

Mock slicer knobs (see mock/orca_cli.py): plate 2 fails, plate 3 slices slowly.
"""
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAIL_PLATE, SLOW_PLATE, SLOW_SECONDS = "2", "3", 6

MACHINE = "Bambu Lab P1S 0.4 nozzle"
PROCESS = "0.20mm Standard @BBL X1E"
FILAMENT = "Bambu PLA Basic @BBL X1E 0.4 nozzle"

# A tetrahedron: the smallest closed mesh stl_to_3mf will accept.
STL = b"""solid t
facet normal 0 0 -1
 outer loop
  vertex 0 0 0
  vertex 10 0 0
  vertex 0 10 0
 endloop
endfacet
facet normal 0 -1 0
 outer loop
  vertex 0 0 0
  vertex 0 0 10
  vertex 10 0 0
 endloop
endfacet
facet normal -1 0 0
 outer loop
  vertex 0 0 0
  vertex 0 10 0
  vertex 0 0 10
 endloop
endfacet
facet normal 1 1 1
 outer loop
  vertex 10 0 0
  vertex 0 0 10
  vertex 0 10 0
 endloop
endfacet
endsolid t
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _shim(path: str, body: str) -> None:
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


@pytest.fixture
def server(tmp_path):
    """Start Laminus; yields {"url", "jobs_dir"} once the profile catalog is built."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _shim(str(bin_dir / "orcaslicer"), f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/mock/orca_cli.py" "$@"\n')
    # Real xvfb-run needs Xvfb; the mock slicer has no display, so just drop its options.
    _shim(str(bin_dir / "xvfb-run"), '#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; done\nexec "$@"\n')

    dirs = {k: tmp_path / k.lower() for k in ("CONFIG_DIR", "DATA_DIR", "JOBS_DIR", "ARRANGE_DIR")}
    port = _free_port()
    env = {
        **os.environ,
        **{k: str(v) for k, v in dirs.items()},
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "USER_CONFIG_DIR": str(dirs["CONFIG_DIR"] / "user"),
        "ORCA_DATADIR": str(dirs["CONFIG_DIR"]),
        "SYSTEM_PROFILES_DIR": os.path.join(ROOT, "tests", "fixtures", "profiles"),
        "MAX_CONCURRENT_JOBS": "1",
        "MOCK_ORCA_FAIL_PLATE": FAIL_PLATE,
        "MOCK_ORCA_SLOW_PLATE": SLOW_PLATE,
        "MOCK_ORCA_SLICE_DELAY": str(SLOW_SECONDS),
    }
    log = open(tmp_path / "server.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen(f"{url}/api/v1", timeout=2) as r:
                    if json.load(r)["ready"]["catalog"]:
                        break
            except OSError:
                pass
            if proc.poll() is not None or time.time() > deadline:
                raise RuntimeError(f"Laminus failed to start:\n{(tmp_path / 'server.log').read_text()}")
            time.sleep(0.2)
        yield {"url": url, "jobs_dir": dirs["JOBS_DIR"]}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture
def stl_file(tmp_path):
    path = tmp_path / "part.stl"
    path.write_bytes(STL)
    return path


@pytest.fixture
def api(server, playwright):
    """Drive the API directly to set up state the UI then has to reflect."""
    ctx = playwright.request.new_context(base_url=server["url"])

    def uuid_of(name):
        page = ctx.get("/api/v1/profiles", params={"q": name, "limit": 200}).json()
        return next(p["uuid"] for p in page["data"] if p["name"] == name)

    profiles = {"machine_uuid": uuid_of(MACHINE), "process_uuid": uuid_of(PROCESS),
                "filament_uuids": json.dumps([uuid_of(FILAMENT)])}

    class Api:
        def create_job(self, plate="1", filename="part.stl"):
            r = ctx.post("/api/v1/jobs", multipart={
                **profiles, "plate": plate,
                "file": {"name": filename, "mimeType": "model/stl", "buffer": STL}})
            assert r.status == 202, r.text()
            return r.json()["id"]

        def wait_for(self, job_id, *statuses, timeout=20):
            deadline = time.time() + timeout
            while time.time() < deadline:
                job = ctx.get(f"/api/v1/jobs/{job_id}").json()
                if job["status"] in statuses:
                    return job
                time.sleep(0.2)
            raise AssertionError(f"job {job_id} never reached {statuses}: {job}")

        def download(self, job_id):
            assert ctx.get(f"/api/v1/jobs/{job_id}/artifact").ok

    yield Api()
    ctx.dispose()
