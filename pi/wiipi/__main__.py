"""Entry point: python -m wiipi"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from aiohttp import web

from .config import Config
from .drive import Drive, FakeDrive
from .gadget import FakeGadget, Gadget
from .service import Service
from .web import create_app

log = logging.getLogger("wiipi")
SETUP_RETRY_SECONDS = 30


def prepare_kernel() -> None:
    """Best effort: load the gadget modules and mount configfs if the host has not."""
    for module in ("dwc2", "libcomposite"):
        subprocess.run(["modprobe", module], capture_output=True)
    configfs = Path("/sys/kernel/config")
    if configfs.is_dir() and not (configfs / "usb_gadget").is_dir():
        subprocess.run(["mount", "-t", "configfs", "none", str(configfs)], capture_output=True)


def bring_up(service: Service, stop: threading.Event) -> None:
    """Initialise the drive, retrying so the web UI can show what is wrong meanwhile."""
    while not stop.is_set():
        try:
            service.initialise()
        except Exception as exc:  # noqa: BLE001
            log.error("setup failed: %s (retrying in %ds)", exc, SETUP_RETRY_SECONDS)
            service.error = f"setup failed: {exc}"
            service.items.clear()
            stop.wait(SETUP_RETRY_SECONDS)
            continue
        service.start()
        return


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    for directory in (cfg.data_dir, cfg.uploads, cfg.ready):
        directory.mkdir(parents=True, exist_ok=True)

    if cfg.fake:
        log.warning("WIIPI_FAKE is set: no USB gadget, using a plain folder as the drive")
        gadget, drive = FakeGadget(), FakeDrive(cfg.data_dir / "fake-drive")
    else:
        prepare_kernel()
        gadget = Gadget(cfg.image, name=cfg.gadget_name, udc=cfg.udc, stall=cfg.stall,
                        detach_mode=cfg.detach_mode)
        drive = Drive(cfg.image, cfg.mount_point, fsck=cfg.fsck)

    service = Service(cfg, gadget, drive)
    stop = threading.Event()
    threading.Thread(target=bring_up, args=(service, stop), name="wiipi-setup", daemon=True).start()
    try:
        web.run_app(create_app(service, cfg), port=cfg.port, print=None, access_log=None)
    finally:
        stop.set()
        service.stop()


if __name__ == "__main__":
    main()
