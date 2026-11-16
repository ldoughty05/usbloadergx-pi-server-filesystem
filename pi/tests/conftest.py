import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiipi.config import Config  # noqa: E402
from wiipi.drive import FakeDrive  # noqa: E402
from wiipi.gadget import FakeGadget  # noqa: E402
from wiipi.service import Service  # noqa: E402

WII_MAGIC = bytes.fromhex("5d1c9ea3")
GC_MAGIC = bytes.fromhex("c2339f3d")


def disc_header(game_id: str, title: str, kind: str = "wii", disc: int = 0) -> bytes:
    header = bytearray(0x100)
    header[0:6] = game_id.encode()
    header[6] = disc
    if kind == "wii":
        header[0x18:0x1C] = WII_MAGIC
    else:
        header[0x1C:0x20] = GC_MAGIC
    raw = title.encode("latin-1")
    header[0x20:0x20 + len(raw)] = raw
    return bytes(header)


def make_iso(path: Path, game_id="RMGE01", title="SUPER MARIO GALAXY", kind="wii", size=8192, disc=0):
    path.write_bytes(disc_header(game_id, title, kind, disc) + b"\x11" * (size - 0x100))
    return path


def make_wbfs(path: Path, game_id="RSPE01", title="Wii Sports", size=8192):
    head = bytearray(512)
    head[0:4] = b"WBFS"
    head[8] = 9   # 512-byte HD sectors
    head[9] = 21  # 2 MiB WBFS sectors
    body = disc_header(game_id, title)
    path.write_bytes(bytes(head) + body + b"\x22" * (size - 512 - len(body)))
    return path


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", image=tmp_path / "data" / "wii.img",
                 inbox=tmp_path / "inbox", mount_point=tmp_path / "mnt", idle_seconds=60)
    cfg.inbox.mkdir()
    gadget = FakeGadget()
    drive = FakeDrive(tmp_path / "drive")
    clock = Clock()
    service = Service(cfg, gadget, drive, clock=clock)
    service.wit = True
    service.initialise()

    class Env:
        pass

    e = Env()
    e.cfg, e.gadget, e.drive, e.clock, e.service, e.tmp = cfg, gadget, drive, clock, service, tmp_path
    return e
