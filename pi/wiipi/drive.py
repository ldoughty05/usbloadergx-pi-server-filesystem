"""The emulated drive: image creation, partition lookup and mounting.

The backing store is either an image file (created on first run) or an existing
block device. It carries an MBR with one FAT32 partition, which is what USB
Loader GX expects from a flash drive.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Iterator

log = logging.getLogger("wiipi.drive")

SECTOR = 512
PART_START = 2048 * SECTOR  # 1 MiB alignment
MIN_IMAGE = 4 * 1024**3
MAX_IMAGE = 0xFFFFFFFF * SECTOR  # MBR limit (~2 TiB)
FAT_TYPES = {0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E}


class DriveError(RuntimeError):
    pass


def build_mbr(total_bytes: int) -> bytes:
    """An MBR holding a single FAT32 (LBA) partition that fills the disk."""
    sectors = (total_bytes - PART_START) // SECTOR
    if sectors <= 0 or sectors > 0xFFFFFFFF:
        raise DriveError("image size out of range for an MBR partition table")
    mbr = bytearray(SECTOR)
    entry = struct.pack(
        "<B3sB3sII",
        0x00,                 # not bootable
        b"\xfe\xff\xff",      # CHS start (unused, LBA)
        0x0C,                 # FAT32 LBA
        b"\xfe\xff\xff",      # CHS end
        PART_START // SECTOR,
        sectors,
    )
    mbr[0x1BE:0x1BE + 16] = entry
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr)


def find_partition(path: Path) -> tuple[int, int]:
    """Return (offset, size) in bytes of the FAT partition inside ``path``.

    Understands MBR, GPT (first partition) and unpartitioned "superfloppy" media.
    A size of 0 means "to the end of the device".
    """
    with open(path, "rb") as fh:
        first = fh.read(SECTOR)
        if len(first) < SECTOR or first[510:512] != b"\x55\xaa":
            raise DriveError(f"{path} has no partition table or FAT boot sector")

        # A FAT boot sector starts with a jump instruction and names its type.
        if first[0] in (0xEB, 0xE9) and (first[82:87] == b"FAT32" or first[54:57] == b"FAT"):
            return 0, 0

        entries = [struct.unpack_from("<B3sB3sII", first, 0x1BE + 16 * i) for i in range(4)]
        if entries[0][2] == 0xEE:  # protective MBR -> GPT
            fh.seek(SECTOR)
            header = fh.read(SECTOR)
            if header[:8] != b"EFI PART":
                raise DriveError(f"{path}: protective MBR without a GPT header")
            table_lba, = struct.unpack_from("<Q", header, 72)
            count, entry_size = struct.unpack_from("<II", header, 80)
            fh.seek(table_lba * SECTOR)
            for _ in range(count):
                raw = fh.read(entry_size)
                if len(raw) < 48 or raw[:16] == bytes(16):
                    continue
                first_lba, last_lba = struct.unpack_from("<QQ", raw, 32)
                return first_lba * SECTOR, (last_lba - first_lba + 1) * SECTOR
            raise DriveError(f"{path}: GPT has no partitions")

        for _status, _chs0, ptype, _chs1, lba, sectors in entries:
            if ptype in FAT_TYPES and sectors:
                return lba * SECTOR, sectors * SECTOR
    raise DriveError(f"{path}: no FAT partition found in the MBR")


def _run(*cmd: str, timeout: float = 600) -> str:
    log.debug("run: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise DriveError(f"{cmd[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise DriveError(f"{cmd[0]} timed out") from exc
    if proc.returncode != 0:
        raise DriveError(f"{' '.join(cmd)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


class Drive:
    """A FAT32 volume inside an image file or block device, mounted on demand."""

    def __init__(self, backing: Path, mount_point: Path, fsck: bool = False):
        self.backing = Path(backing)
        self.mount_point = Path(mount_point)
        self.fsck = fsck
        #: True while the volume is mounted here. If an unmount ever fails this
        #: stays True, and the caller must not hand the drive back to the Wii.
        self.is_mounted = False

    def exists(self) -> bool:
        return self.backing.exists()

    def create(self, size: int) -> None:
        """Create a sparse image with an MBR and a freshly formatted FAT32 volume."""
        if not MIN_IMAGE <= size <= MAX_IMAGE:
            raise DriveError("WIIPI_IMAGE_SIZE must be between 4G and 2T")
        size -= size % (1024**2)
        self.backing.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.backing.parent).free
        if size > free:
            log.warning(
                "image size (%d GiB) is larger than the free space (%d GiB); the image is "
                "sparse, but the disk will fill up before the Wii drive does",
                size // 1024**3, free // 1024**3,
            )
        tmp = self.backing.with_suffix(self.backing.suffix + ".creating")
        log.info("creating %d GiB drive image at %s", size // 1024**3, self.backing)
        with open(tmp, "wb") as fh:
            fh.truncate(size)
            fh.write(build_mbr(size))
        loop = _run("losetup", "--find", "--show", "--offset", str(PART_START),
                    "--sizelimit", str(size - PART_START), str(tmp))
        try:
            # 32 KiB clusters: what Wii loaders expect for fast, unfragmented access.
            _run("mkfs.fat", "-F", "32", "-s", "64", "-n", "WIIPI", loop)
        finally:
            _run("losetup", "-d", loop)
        with self._mounted_path(tmp) as root:
            (root / "wbfs").mkdir(exist_ok=True)
            (root / "games").mkdir(exist_ok=True)
        os.replace(tmp, self.backing)

    @contextlib.contextmanager
    def _mounted_path(self, path: Path) -> Iterator[Path]:
        if self.is_mounted:  # left over from a failed unmount: try again first
            _run("umount", str(self.mount_point))
            self.is_mounted = False
        offset, size = find_partition(path)
        cmd = ["losetup", "--find", "--show", "--offset", str(offset)]
        if size:
            cmd += ["--sizelimit", str(size)]
        loop = _run(*cmd, str(path))
        mounted = False
        try:
            if self.fsck:
                with contextlib.suppress(DriveError):
                    _run("fsck.fat", "-a", loop, timeout=1800)
            self.mount_point.mkdir(parents=True, exist_ok=True)
            _run("mount", "-t", "vfat", "-o", "rw,noatime,utf8=1,shortname=mixed", loop,
                 str(self.mount_point))
            mounted = self.is_mounted = True
            yield self.mount_point
        finally:
            try:
                if mounted:
                    os.sync()
                    _run("umount", str(self.mount_point))
                    self.is_mounted = False
            finally:
                # Detaches now, or as soon as the last user goes away.
                _run("losetup", "-d", loop)

    @contextlib.contextmanager
    def mounted(self) -> Iterator[Path]:
        """Mount the volume read-write. The gadget MUST be detached first."""
        with self._mounted_path(self.backing) as root:
            yield root


class FakeDrive:
    """A plain directory standing in for the drive (development and tests)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.backing = self.root
        self.mount_count = 0
        self.is_mounted = False

    def exists(self) -> bool:
        return self.root.is_dir()

    def create(self, size: int) -> None:
        (self.root / "wbfs").mkdir(parents=True, exist_ok=True)
        (self.root / "games").mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def mounted(self) -> Iterator[Path]:
        self.mount_count += 1
        self.is_mounted = True
        try:
            yield self.root
        finally:
            self.is_mounted = False
