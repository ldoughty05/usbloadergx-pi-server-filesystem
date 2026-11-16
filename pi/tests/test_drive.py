import struct

import pytest

from wiipi.config import parse_size
from wiipi.drive import PART_START, SECTOR, DriveError, build_mbr, find_partition


def test_parse_size():
    assert parse_size("64G") == 64 * 1024**3
    assert parse_size("500m") == 500 * 1024**2
    assert parse_size("1.5T") == int(1.5 * 1024**4)
    assert parse_size("4096") == 4096
    assert parse_size("8GiB") == 8 * 1024**3
    with pytest.raises(ValueError):
        parse_size("lots")


def test_mbr_roundtrip(tmp_path):
    size = 8 * 1024**3
    image = tmp_path / "wii.img"
    with open(image, "wb") as fh:
        fh.truncate(size)
        fh.write(build_mbr(size))
    with open(image, "rb") as fh:
        mbr = fh.read(SECTOR)
    assert mbr[510:512] == b"\x55\xaa"
    assert mbr[0x1BE + 4] == 0x0C  # FAT32 LBA, what the loader looks for
    assert find_partition(image) == (PART_START, size - PART_START)


def test_superfloppy(tmp_path):
    boot = bytearray(SECTOR)
    boot[0] = 0xEB
    boot[82:90] = b"FAT32   "
    boot[510:512] = b"\x55\xaa"
    image = tmp_path / "floppy.img"
    image.write_bytes(bytes(boot) + bytes(SECTOR))
    assert find_partition(image) == (0, 0)


def test_gpt(tmp_path):
    mbr = bytearray(SECTOR)
    mbr[0x1BE + 4] = 0xEE
    mbr[510:512] = b"\x55\xaa"
    header = bytearray(SECTOR)
    header[:8] = b"EFI PART"
    struct.pack_into("<Q", header, 72, 2)        # partition table at LBA 2
    struct.pack_into("<II", header, 80, 4, 128)  # 4 entries of 128 bytes
    entry = bytearray(128)
    entry[:16] = b"\x01" * 16
    struct.pack_into("<QQ", entry, 32, 2048, 2048 + 999)
    image = tmp_path / "gpt.img"
    image.write_bytes(bytes(mbr) + bytes(header) + bytes(entry) + bytes(3 * 128))
    assert find_partition(image) == (2048 * SECTOR, 1000 * SECTOR)


def test_unformatted(tmp_path):
    image = tmp_path / "blank.img"
    image.write_bytes(bytes(4096))
    with pytest.raises(DriveError):
        find_partition(image)
    mbr = bytearray(SECTOR)
    mbr[0x1BE + 4] = 0x83  # Linux partition only
    struct.pack_into("<II", mbr, 0x1BE + 8, 2048, 1000)
    mbr[510:512] = b"\x55\xaa"
    image.write_bytes(bytes(mbr))
    with pytest.raises(DriveError):
        find_partition(image)
