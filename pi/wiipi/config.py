"""Runtime configuration, read from WIIPI_* environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(text: str) -> int:
    """Parse '64G', '500M', '1073741824' into bytes."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:I?B)?\s*", text.upper())
    if not m:
        raise ValueError(f"not a size: {text!r}")
    return int(float(m.group(1)) * _UNITS[m.group(2)])


def _flag(env: dict, name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    data_dir: Path = Path("/data")
    image: Path = Path("/data/wii.img")
    image_size: int = 64 * 1024**3
    inbox: Path = Path("/inbox")
    mount_point: Path = Path("/mnt/wii")
    port: int = 8765
    token: str = ""
    udc: str = ""
    gadget_name: str = "wiipi"
    stall: bool = False
    detach_mode: str = "unplug"
    idle_seconds: float = 60.0
    auto_install: bool = True
    fsck: bool = False
    fake: bool = False
    wit_args: str = ""

    @property
    def staging(self) -> Path:
        return self.data_dir / "staging"

    @property
    def uploads(self) -> Path:
        return self.staging / "uploads"

    @property
    def ready(self) -> Path:
        return self.staging / "ready"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        data_dir = Path(env.get("WIIPI_DATA", "/data"))
        return cls(
            data_dir=data_dir,
            image=Path(env.get("WIIPI_IMAGE", str(data_dir / "wii.img"))),
            image_size=parse_size(env.get("WIIPI_IMAGE_SIZE", "64G")),
            inbox=Path(env.get("WIIPI_INBOX", "/inbox")),
            mount_point=Path(env.get("WIIPI_MOUNT", "/mnt/wii")),
            port=int(env.get("WIIPI_PORT", "8765")),
            token=env.get("WIIPI_TOKEN", ""),
            udc=env.get("WIIPI_UDC", ""),
            gadget_name=env.get("WIIPI_GADGET_NAME", "wiipi"),
            stall=_flag(env, "WIIPI_STALL", False),
            detach_mode="eject" if env.get("WIIPI_DETACH_MODE", "").strip().lower() == "eject" else "unplug",
            idle_seconds=float(env.get("WIIPI_IDLE_SECONDS", "60")),
            auto_install=_flag(env, "WIIPI_AUTO_INSTALL", True),
            fsck=_flag(env, "WIIPI_FSCK", False),
            fake=_flag(env, "WIIPI_FAKE", False),
            wit_args=env.get("WIIPI_WIT_ARGS", ""),
        )
