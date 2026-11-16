<p align="center"><a href="https://github.com/wiidev/usbloadergx/" title="USB Loader GX"><img src="data/web/logo.png"></a></p>
<p align="center">
<a href="https://github.com/wiidev/usbloadergx/releases" title="Releases"><img src="https://img.shields.io/github/v/release/wiidev/usbloadergx?logo=github"></a>
<a href="https://github.com/wiidev/usbloadergx/actions" title="Actions"><img src="https://img.shields.io/github/actions/workflow/status/wiidev/usbloadergx/main.yml?branch=enhanced&logo=github"></a>
</p>

## Description
USB Loader GX allows you to play Wii and GameCube games from a USB storage device or an SD card, launch other homebrew apps, create backups, use cheats in games, and a whole lot more.

## This fork: Raspberry Pi as the USB drive (Pi Sync)
This is a modified version of [wiidev/usbloadergx](https://github.com/wiidev/usbloadergx). It adds **Pi Sync**: a Raspberry Pi cabled to the Wii poses as the USB drive, and games added to the Pi remotely are installed without unplugging anything.

- [`pi/`](pi/README.md) is the Docker service that runs on the Pi (USB drive emulation, web UI, install queue). Setup instructions are there.
- In the loader, *Settings > Hard Drive Settings* has four new entries: `Pi Sync`, `Pi Sync Address`, `Pi Sync Port` and `Pi Sync Now`. With Pi Sync on, the loader asks before handing the drive to the Pi, then remounts it and reloads the game list.

The loader changes are in `source/network/PiSync.cpp`, with small hooks in `menu.cpp`, `menu/GameBrowseMenu.cpp`, `settings/CSettings.*` and `settings/menus/HardDriveSM.cpp`.

## Installation
1. Extract the apps folder to the root of your SD card and replace any existing files.
2. Install the [latest d2x cIOS](https://github.com/wiidev/d2x-cios/releases).
3. Optional: Update wiitdb.xml by selecting the update option within the loaders settings menu.
4. Optional: Install the loaders [forwarder channel](https://raw.githubusercontent.com/wiidev/usbloadergx/updates/USBLoaderGX_forwarder%5BUNEO%5D.wad), then go into `Loader Settings` and set `Return To` to `UNEO`.

## d2x cIOS
1. Use the correct cIOS package for your console — e.g., `d2x-v11-beta3` for the Wii and `d2x-v11-beta3-vWii` for the Wii U.
2. When using the d2x cIOS installer, set the cIOS to the version that you downloaded — e.g., `d2x-v11-beta3`.
3. Install the cIOS into each slot with the following settings.

````
Slot 248 base 38
Slot 249 base 56
Slot 250 base 57
Slot 251 base 58
````
