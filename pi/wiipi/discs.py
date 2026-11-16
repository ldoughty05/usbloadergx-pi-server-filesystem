"""Disc image handling: identify, convert and lay out games the way USB Loader GX expects.

Layout on the FAT32 drive:
    wbfs/<Title> [<ID6>]/<ID6>.wbfs  (+ .wbf1, .wbf2 ... when larger than the split size)
    games/<Title> [<ID6>]/game.iso   (GameCube, for Nintendont; disc 2 is disc2.iso)
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("wiipi.discs")

WII_MAGIC = bytes.fromhex("5d1c9ea3")
GC_MAGIC = bytes.fromhex("c2339f3d")
# One 32 KiB cluster less than 4 GiB: the same split USB Loader GX itself writes.
SPLIT_SIZE = 4 * 1024**3 - 32 * 1024
COPY_CHUNK = 4 * 1024**2
# Formats wit can read and turn into WBFS for us.
WIT_EXTENSIONS = {".iso", ".ciso", ".wdf", ".wia", ".gcm"}
EXTENSIONS = WIT_EXTENSIONS | {".wbfs"}

Progress = Callable[[int, int], None]


class DiscError(RuntimeError):
    pass


@dataclass
class DiscInfo:
    kind: str        # "wii" or "gc"
    container: str   # "iso" or "wbfs"
    game_id: str
    title: str
    disc_number: int = 0

    @property
    def folder(self) -> str:
        return f"{clean_title(self.title)} [{self.game_id}]"

    @property
    def dest_dir(self) -> str:
        return f"{'wbfs' if self.kind == 'wii' else 'games'}/{self.folder}"


@dataclass
class Prepared:
    info: DiscInfo
    directory: Path                              # staging directory holding the files
    files: list[str] = field(default_factory=list)   # file names inside ``directory``
    size: int = 0


def clean_title(title: str) -> str:
    """Make a disc title safe as a FAT directory name."""
    text = "".join(ch for ch in title if 32 <= ord(ch) < 127)
    text = re.sub(r'[\\/:*?"<>|]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:64] or "Game"


def _parse_header(header: bytes, container: str) -> DiscInfo:
    if len(header) < 0x60:
        raise DiscError("file is too small to be a disc image")
    if header[0x18:0x1C] == WII_MAGIC:
        kind = "wii"
    elif header[0x1C:0x20] == GC_MAGIC:
        kind = "gc"
    else:
        raise DiscError("not a Wii or GameCube disc image")
    raw_id = header[:6]
    if not re.fullmatch(rb"[A-Z0-9]{6}", raw_id):
        raise DiscError("disc image has an invalid game ID")
    title = header[0x20:0x60].split(b"\0", 1)[0].decode("latin-1").strip()
    return DiscInfo(kind, container, raw_id.decode(), title or raw_id.decode(), header[6])


def identify(path: Path) -> DiscInfo:
    """Read the game ID and title from an ISO or WBFS file."""
    with open(path, "rb") as fh:
        head = fh.read(0x100)
        if head[:4] == b"WBFS":
            # The disc header copy sits at the start of the second "HD sector".
            sector_shift = head[8]
            if not 9 <= sector_shift <= 12:
                raise DiscError("corrupt WBFS header")
            fh.seek(1 << sector_shift)
            return _parse_header(fh.read(0x100), "wbfs")
        return _parse_header(head, "iso")


def have_wit() -> bool:
    return shutil.which("wit") is not None


def split_into(src: Path, dest_dir: Path, base_name: str, progress: Optional[Progress] = None,
               split_size: int = SPLIT_SIZE, move: bool = False) -> list[str]:
    """Write ``src`` as base.wbfs, base.wbf1, ... in ``dest_dir``; return the file names."""
    total = src.stat().st_size
    dest_dir.mkdir(parents=True, exist_ok=True)
    if total <= split_size:
        target = dest_dir / f"{base_name}.wbfs"
        if move:
            try:
                os.replace(src, target)
                if progress:
                    progress(total, total)
                return [target.name]
            except OSError:
                pass  # different filesystem: fall through to a copy
        copy_file(src, target, progress)
        if move:
            src.unlink(missing_ok=True)
        return [target.name]

    names: list[str] = []
    done = 0
    with open(src, "rb") as fin:
        index = 0
        while done < total:
            if index > 9:
                raise DiscError("image needs more than 10 split files")
            name = f"{base_name}.wbfs" if index == 0 else f"{base_name}.wbf{index}"
            names.append(name)
            written = 0
            with open(dest_dir / name, "wb") as fout:
                while written < split_size:
                    chunk = fin.read(min(COPY_CHUNK, split_size - written))
                    if not chunk:
                        break
                    fout.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(done + written, total)
            done += written
            index += 1
    if move:
        src.unlink(missing_ok=True)
    return names


def copy_file(src: Path, dst: Path, progress: Optional[Progress] = None, base: int = 0,
              total: Optional[int] = None, sync: bool = False) -> int:
    """Chunked copy with progress reporting. Returns the number of bytes copied."""
    size = src.stat().st_size
    total = size if total is None else total
    copied = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(COPY_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            copied += len(chunk)
            if progress:
                progress(base + copied, total)
        if sync:
            fout.flush()
            os.fsync(fout.fileno())
    return copied


def wit_convert(src: Path, dst: Path, extra_args: str = "") -> None:
    """Convert any image wit understands into a scrubbed, single-file WBFS."""
    cmd = ["wit", "copy", "--wbfs", "--overwrite", *shlex.split(extra_args),
           "--source", str(src), "--dest", str(dst)]
    log.info("converting with wit: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        dst.unlink(missing_ok=True)
        detail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:] or ["unknown error"]
        raise DiscError(f"wit could not convert this image: {detail[0]}")


def prepare(src: Path, work_dir: Path, progress: Optional[Progress] = None,
            wit_args: str = "") -> Prepared:
    """Turn an uploaded file into its final on-drive files inside ``work_dir``.

    ``src`` is consumed (moved or deleted) on success.
    """
    ext = src.suffix.lower()
    try:
        info: Optional[DiscInfo] = identify(src)
    except DiscError:
        if ext not in WIT_EXTENSIONS - {".iso", ".gcm"}:
            raise
        info = None  # compressed container: only wit can read it

    work_dir.mkdir(parents=True, exist_ok=True)

    if info and info.kind == "gc":
        if info.container != "iso":
            raise DiscError("GameCube games must be plain .iso files")
        name = "game.iso" if info.disc_number == 0 else f"disc{info.disc_number + 1}.iso"
        try:
            os.replace(src, work_dir / name)
            if progress:
                size = (work_dir / name).stat().st_size
                progress(size, size)
        except OSError:
            copy_file(src, work_dir / name, progress)
            src.unlink(missing_ok=True)
        files = [name]
    elif info and info.container == "wbfs":
        files = split_into(src, work_dir, info.game_id, progress, move=True)
    else:
        if not have_wit():
            raise DiscError("this format needs Wiimms ISO Tools (wit), which is not installed; "
                            "upload a .wbfs file instead")
        converted = work_dir / "converted.tmp"
        wit_convert(src, converted, wit_args)
        info = identify(converted)
        if info.kind != "wii":
            converted.unlink(missing_ok=True)
            raise DiscError("only Wii games can be converted to WBFS")
        files = split_into(converted, work_dir, info.game_id, progress, move=True)
        src.unlink(missing_ok=True)

    size = sum((work_dir / name).stat().st_size for name in files)
    return Prepared(info, work_dir, files, size)
