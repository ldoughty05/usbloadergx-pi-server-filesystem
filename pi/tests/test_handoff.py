"""End to end: the loader's PiSync.cpp (built for the host) against a running wiipi service."""

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import make_wbfs

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PISYNC = REPO / "source" / "network" / "PiSync.cpp"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    if not shutil.which("g++") or not PISYNC.exists():
        pytest.skip("needs g++ and the loader sources")
    out = tmp_path_factory.mktemp("harness") / "harness"
    subprocess.run(
        ["g++", "-std=gnu++17", "-D_GNU_SOURCE", "-Wall", "-Wno-unused-parameter",
         "-I", str(HERE / "handoff" / "stubs"), "-I", str(REPO / "source"),
         "-I", str(REPO / "source" / "network"),
         str(HERE / "handoff" / "harness.cpp"), str(PISYNC), "-o", str(out)],
        check=True)
    return out


@pytest.fixture
def server(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, WIIPI_FAKE="1", WIIPI_DATA=str(tmp_path / "data"),
               WIIPI_INBOX=str(tmp_path / "inbox"), WIIPI_PORT=str(port),
               WIIPI_IDLE_SECONDS="99999")  # only the loader's release can start a sync
    proc = subprocess.Popen([sys.executable, "-m", "wiipi"], cwd=HERE.parent, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if call(port, "GET", "/api/status")["gen"] >= 1:
                    break
            except (OSError, KeyError):
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("wiipi did not start")
        yield port, tmp_path
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"} if isinstance(body, dict) else {}
    conn.request(method, path, json.dumps(body) if isinstance(body, dict) else body, headers)
    data = conn.getresponse().read()
    conn.close()
    return json.loads(data)


def run(harness, port, mode):
    return subprocess.run([str(harness), "127.0.0.1", str(port), mode],
                          capture_output=True, text=True, timeout=120)


def test_loader_and_pi_hand_the_drive_over(harness, server):
    port, tmp = server
    game = tmp / "data" / "fake-drive" / "wbfs" / "SUPER MARIO GALAXY [RMGE01]" / "RMGE01.wbfs"

    quiet = run(harness, port, "idle")
    assert quiet.returncode == 0, quiet.stdout  # nothing queued: no prompt

    data = make_wbfs(tmp / "up.wbfs", "RMGE01", "SUPER MARIO GALAXY", size=3 * 1024 * 1024).read_bytes()
    upload = call(port, "POST", "/api/uploads", {"name": "smg.wbfs", "size": len(data)})["id"]
    call(port, "PUT", f"/api/uploads/{upload}?offset=0", data)
    call(port, "POST", f"/api/uploads/{upload}/complete", b"")
    for _ in range(100):
        if call(port, "GET", "/api/status")["items"][0]["status"] == "ready":
            break
        time.sleep(0.1)
    time.sleep(1.5)
    assert not game.exists()  # the Pi keeps its hands off while the Wii owns the drive

    result = run(harness, port, "sync")
    assert result.returncode == 0, result.stdout
    assert "1 change(s) are waiting" in result.stdout
    order = result.stdout.split("ORDER ")[1].strip().rstrip(",").split(",")
    # The loader lets go of the drive before the Pi works, and only remounts afterwards.
    assert order.index("WBFS_CloseAll") < order.index("UnMountAllUSB") < order.index("MountAllUSB")
    assert order.index("MountAllUSB") < order.index("ReadGameList")
    assert game.read_bytes() == data

    status = call(port, "GET", "/api/status")
    assert status["drive"]["attached"] and not status["drive"]["busy"]
    assert status["installed"][0]["id"] == "RMGE01"


def test_loader_gives_up_quickly_when_pi_is_down(harness):
    started = time.monotonic()
    result = run(harness, 1, "unreachable")  # nothing listens on port 1
    assert result.returncode == 0 and time.monotonic() - started < 5
