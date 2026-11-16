#!/bin/sh
# One-time host setup for wiipi on Raspberry Pi OS (Pi 4 / Pi 5 / Zero 2 W).
# Puts the USB-C (or Zero data) port into device mode so the Wii sees a drive.
# Run with sudo, then reboot.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo." >&2
    exit 1
fi

CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
LINE="dtoverlay=dwc2,dr_mode=peripheral"

if grep -q "^dtoverlay=dwc2" "$CONFIG"; then
    echo "A dwc2 overlay is already in $CONFIG:"
    grep "^dtoverlay=dwc2" "$CONFIG"
    echo "Make sure it reads: $LINE"
else
    cp "$CONFIG" "$CONFIG.wiipi-backup"
    printf '\n[all]\n# wiipi: USB device (gadget) mode on the USB-C port\n%s\n' "$LINE" >> "$CONFIG"
    echo "Added '$LINE' to $CONFIG (backup: $CONFIG.wiipi-backup)"
fi

printf 'dwc2\nlibcomposite\n' > /etc/modules-load.d/wiipi.conf
echo "Wrote /etc/modules-load.d/wiipi.conf"
echo
echo "Reboot, then check that 'ls /sys/class/udc' prints a controller name."
