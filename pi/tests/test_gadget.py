import pytest

from wiipi.gadget import Gadget, GadgetError


@pytest.fixture
def sysfs(tmp_path):
    configfs = tmp_path / "config"
    (configfs / "usb_gadget").mkdir(parents=True)
    udc = tmp_path / "udc"
    (udc / "1000480000.usb").mkdir(parents=True)
    (udc / "1000480000.usb" / "state").write_text("not attached\n")
    return configfs, udc


def test_setup_attach_detach(tmp_path, sysfs):
    configfs, udc = sysfs
    image = tmp_path / "wii.img"
    image.touch()
    gadget = Gadget(image, configfs=configfs, udc_class=udc)
    gadget.setup()
    root = configfs / "usb_gadget" / "wiipi"
    assert (root / "functions/mass_storage.usb0/lun.0/removable").read_text() == "1\n"
    assert (root / "functions/mass_storage.usb0/stall").read_text() == "0\n"
    assert (root / "configs/c.1/mass_storage.usb0").is_symlink()
    assert not gadget.attached and gadget.host_state() == "detached"

    gadget.attach()
    assert (root / "UDC").read_text() == "1000480000.usb\n"
    assert (root / "functions/mass_storage.usb0/lun.0/file").read_text().strip() == str(image)
    assert gadget.attached and gadget.host_state() == "not attached"
    (udc / "1000480000.usb" / "state").write_text("configured\n")
    assert gadget.host_state() == "configured"

    gadget.detach()
    assert (root / "UDC").read_text().strip() == ""
    assert (root / "functions/mass_storage.usb0/lun.0/file").read_text().strip() == ""
    assert not gadget.attached


def test_setup_adopts_live_gadget(tmp_path, sysfs):
    configfs, udc = sysfs
    gadget = Gadget(tmp_path / "wii.img", configfs=configfs, udc_class=udc)
    gadget.setup()
    gadget.attach()
    marker = configfs / "usb_gadget/wiipi/strings/0x409/product"
    marker.write_text("in use\n")
    again = Gadget(tmp_path / "wii.img", configfs=configfs, udc_class=udc)
    again.setup()  # must not rewrite or unbind a gadget the Wii may be using
    assert marker.read_text() == "in use\n" and again.attached


def test_missing_controller(tmp_path):
    configfs = tmp_path / "config"
    (configfs / "usb_gadget").mkdir(parents=True)
    (tmp_path / "udc").mkdir()
    gadget = Gadget(tmp_path / "wii.img", configfs=configfs, udc_class=tmp_path / "udc")
    gadget.setup()
    with pytest.raises(GadgetError, match="dwc2"):
        gadget.attach()
    with pytest.raises(GadgetError, match="libcomposite"):
        Gadget(tmp_path / "x.img", configfs=tmp_path / "nope", udc_class=tmp_path / "udc").setup()


def test_eject_mode_keeps_the_usb_link(tmp_path, sysfs):
    configfs, udc = sysfs
    gadget = Gadget(tmp_path / "wii.img", configfs=configfs, udc_class=udc, detach_mode="eject")
    gadget.setup()
    gadget.attach()
    root = configfs / "usb_gadget" / "wiipi"
    gadget.detach()
    assert (root / "UDC").read_text().strip() == "1000480000.usb"   # still on the bus
    assert (root / "functions/mass_storage.usb0/lun.0/file").read_text().strip() == ""
    assert not gadget.attached and gadget.host_state() == "detached"
    gadget.attach()
    assert gadget.attached
    assert (root / "functions/mass_storage.usb0/lun.0/file").read_text().strip() == str(tmp_path / "wii.img")
