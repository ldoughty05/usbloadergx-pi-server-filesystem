"""USB mass-storage gadget control through Linux configfs.

The gadget makes the Pi's USB-C (device-mode) port look like a flash drive whose
contents are the backing image. "Detaching" unbinds the gadget from the USB
device controller, which the Wii sees as the drive being unplugged; only then
is it safe for the Pi to touch the image.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("wiipi.gadget")


class GadgetError(RuntimeError):
    pass


class Gadget:
    def __init__(
        self,
        backing: Path,
        name: str = "wiipi",
        udc: str = "",
        stall: bool = False,
        detach_mode: str = "unplug",
        configfs: Path = Path("/sys/kernel/config"),
        udc_class: Path = Path("/sys/class/udc"),
    ):
        self.backing = Path(backing)
        self.udc_name = udc
        self.stall = stall
        #: "unplug": drop off the USB bus while the Pi works (like pulling the cable).
        #: "eject": stay on the bus and only remove the medium (like a card reader).
        self.detach_mode = detach_mode
        self.udc_class = Path(udc_class)
        self.root = Path(configfs) / "usb_gadget" / name
        self.function = self.root / "functions" / "mass_storage.usb0"
        self.lun = self.function / "lun.0"
        self.config = self.root / "configs" / "c.1"

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _write(path: Path, value: str, optional: bool = False) -> None:
        try:
            path.write_text(value + "\n")
        except OSError as exc:
            if optional:
                log.debug("optional attribute %s not set: %s", path, exc)
                return
            raise GadgetError(f"cannot write {value!r} to {path}: {exc}") from exc

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    def find_udc(self) -> str:
        if self.udc_name:
            return self.udc_name
        try:
            names = sorted(p.name for p in self.udc_class.iterdir())
        except OSError:
            names = []
        if not names:
            raise GadgetError(
                "no USB device controller found in /sys/class/udc. Add "
                "'dtoverlay=dwc2,dr_mode=peripheral' to /boot/firmware/config.txt and reboot."
            )
        self.udc_name = names[0]
        return self.udc_name

    # -- lifecycle -------------------------------------------------------
    def setup(self) -> None:
        """Create the gadget if it does not exist yet. Never disturbs a live one."""
        if not self.root.parent.is_dir():
            raise GadgetError(
                f"{self.root.parent} is missing: load the 'libcomposite' module and mount configfs."
            )
        if self.lun.is_dir() and (self.config / "mass_storage.usb0").exists():
            log.info("adopting existing gadget at %s", self.root)
            return

        log.info("creating USB gadget at %s", self.root)
        strings = self.root / "strings" / "0x409"
        cfg_strings = self.config / "strings" / "0x409"
        for directory in (self.root, strings, self.config, cfg_strings, self.function, self.lun):
            directory.mkdir(parents=True, exist_ok=True)

        self._write(self.root / "idVendor", "0x1d6b")  # Linux Foundation
        self._write(self.root / "idProduct", "0x0104")  # Multifunction composite gadget
        self._write(self.root / "bcdDevice", "0x0100")
        self._write(self.root / "bcdUSB", "0x0200")
        self._write(strings / "serialnumber", "WIIPI0001")
        self._write(strings / "manufacturer", "wiipi")
        self._write(strings / "product", "Wii Game Drive")
        self._write(cfg_strings / "configuration", "Mass Storage")
        self._write(self.config / "bmAttributes", "0xc0", optional=True)  # self powered
        self._write(self.config / "MaxPower", "2", optional=True)

        self._write(self.function / "stall", "1" if self.stall else "0", optional=True)
        self._write(self.lun / "removable", "1")
        self._write(self.lun / "ro", "0")
        self._write(self.lun / "cdrom", "0")
        self._write(self.lun / "nofua", "1", optional=True)
        self._write(self.lun / "inquiry_string", "wiipi   Wii Game Drive  0001", optional=True)

        link = self.config / "mass_storage.usb0"
        if not link.exists():
            link.symlink_to(self.function)

    @property
    def bound(self) -> bool:
        return bool(self._read(self.root / "UDC"))

    @property
    def attached(self) -> bool:
        """True while the host can read the drive (on the bus, medium present)."""
        return self.bound and bool(self._read(self.lun / "file"))

    def attach(self) -> None:
        """Open the backing image and present the drive to the USB host."""
        if self.attached:
            return
        if self._read(self.lun / "file") != str(self.backing):
            self._write(self.lun / "file", str(self.backing))
        if not self.bound:
            self._write(self.root / "UDC", self.find_udc())
        log.info("drive attached to USB host")

    def detach(self) -> None:
        """Take the drive away from the USB host and release the backing image."""
        if self.detach_mode == "eject" and self.bound:
            try:
                self._write(self.lun / "file", "")
                log.info("medium ejected (still on the USB bus)")
                return
            except GadgetError as exc:
                # The host locked the medium: fall back to unplugging.
                log.warning("eject refused (%s); unplugging instead", exc)
        if self.bound:
            self._write(self.root / "UDC", "")
            # Give the kernel a moment to tear the function down before we
            # close the backing file (closing also flushes it to disk).
            time.sleep(0.5)
        if self._read(self.lun / "file"):
            self._write(self.lun / "file", "")
        log.info("drive detached from USB host")

    def host_state(self) -> str:
        """UDC state: 'configured' means a host is actively using the drive."""
        if not self.attached:
            return "detached"
        try:
            udc = self.find_udc()
        except GadgetError:
            return "unknown"
        return self._read(self.udc_class / udc / "state") or "unknown"


class FakeGadget:
    """Stand-in for development and tests (no kernel involved)."""

    def __init__(self):
        self.attached = False
        self.state = "not attached"
        self.events: list[str] = []
        self.fail_detach = False

    def setup(self) -> None:
        self.events.append("setup")

    def attach(self) -> None:
        self.attached = True
        self.events.append("attach")

    def detach(self) -> None:
        if self.fail_detach:
            raise GadgetError("simulated detach failure")
        self.attached = False
        self.events.append("detach")

    def host_state(self) -> str:
        return self.state if self.attached else "detached"
