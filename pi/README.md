# wiipi: a Raspberry Pi as the Wii's USB drive

`wiipi` runs in Docker on a Raspberry Pi that is cabled to a Wii. The Wii sees an ordinary
USB flash drive. You add games from a browser (or by copying files to the Pi) and they
show up in USB Loader GX without unplugging anything. The Pi keeps running whatever else
it runs.

It has two halves:

| Part | Where | What it does |
|---|---|---|
| `pi/` (this folder) | Docker container on the Pi | Emulates the USB drive, takes uploads, converts and installs games |
| **Pi Sync** in USB Loader GX | The loader built from this fork | Lets go of the drive when the Pi asks, then remounts it and reloads the game list |

Use it with backups of discs you own.

## How installing works

The Wii and the Pi must never use the drive at the same time: the loader caches the
filesystem and a running game reads raw sectors. So the drive always has exactly one owner.

1. An upload lands in a staging area on the Pi and is prepared there (ISO to WBFS,
   split at 4 GB). The Wii is not disturbed.
2. When a game is ready, the Pi waits for one of:
   - **The Wii is off** (no USB host has the drive configured for `WIIPI_IDLE_SECONDS`): it installs right away.
   - **USB Loader GX releases the drive**: the loader polls the Pi every 15 seconds, asks
     "Install now?", unmounts the drive and tells the Pi to go ahead.
   - **You press "Install now" in the web UI**: this unplugs the drive regardless. A running game will freeze or crash.
3. The Pi unplugs the virtual drive, mounts the image, copies the game in, unmounts and plugs it back in.
4. The loader mounts the drive again and reloads its lists.

If the Wii is in a game, nothing happens until you are back in the loader or the console is off.

## Hardware

- A Pi with a device-mode USB port: **Pi 4 / Pi 5** (the USB-C port) or **Zero / Zero 2 W** (the data micro-USB port).
- A USB-A to USB-C **data** cable from the Wii to that port. Use the Wii's USB port 0 (the one nearest the edge of the console) unless you changed the loader's USB port setting.
- **Power for a Pi 4/5 from somewhere else.** The USB-C port is also the power input and a Wii port cannot power these boards. Power the Pi through the GPIO header or a PoE HAT, or use a USB-C power/data splitter. A Zero can run from the Wii's port directly.
- The Pi and the Wii on the same LAN (the Wii's Wi-Fi is fine) for Pi Sync. Give the Pi a fixed IP address.

## Setup

### 1. Pi host (once)

```sh
sudo ./host-setup.sh   # adds dtoverlay=dwc2,dr_mode=peripheral and loads dwc2 + libcomposite at boot
sudo reboot
ls /sys/class/udc      # must print a controller name after the reboot
```

### 2. Container

```sh
cd pi
docker compose up -d --build
docker compose logs -f     # first start creates and formats data/wii.img
```

Then open `http://<pi-address>:8765`.

The image is a sparse file: `WIIPI_IMAGE_SIZE` is the size the Wii sees, and disk space on
the Pi is only used as games are added. Uploads also need staging space on the Pi while
they wait (the size of the game).

To add it to an existing compose stack, copy the `wiipi` service from `docker-compose.yml`.
The container is `privileged` because it drives the kernel's USB gadget and loop-mounts the image.

### 3. USB Loader GX

1. Build the loader from this fork: `docker build -o . .` in the repository root writes `usbloader_gx.zip`; copy its `apps/usbloader_gx` folder to the SD card. (GitHub Actions also builds every push; set the repository variable `ALLOW_UPLOADS` to `true` to get the `boot.dol` as an artifact.) Keeping the loader and its settings on the SD card is recommended.
2. Start it with the Pi connected. It should find the drive like any other.
3. *Settings > Hard Drive Settings*:
   - **Pi Sync Address**: the Pi's IP address
   - **Pi Sync Port**: `8765`
   - **Pi Sync**: ON
   - **Pi Sync Now** checks the connection and installs anything waiting.

## Adding games

- **Web UI**: "Add games", pick `.wbfs` or `.iso` files. Uploads are sent in 8 MB pieces and resume after a dropped connection, so they also work through a tunnel or reverse proxy.
- **Inbox folder**: copy files into `pi/inbox/` on the Pi (scp, rsync, a network share). They are picked up once they stop growing.

Wii games go to `wbfs/Title [GAMEID]/GAMEID.wbfs`; GameCube `.iso` files go to
`games/Title [GAMEID]/game.iso` for Nintendont. Formats other than `.wbfs` are converted with
[Wiimms ISO Tools](https://wit.wiimm.de/) (`wit`), which is in the image.

Removing a game in the web UI is queued the same way and applied at the next install.

## Settings

Environment variables in `docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `WIIPI_IMAGE_SIZE` | `64G` | Size of the virtual drive (4G to 2T). Only used when the image is created. |
| `WIIPI_IMAGE` | `/data/wii.img` | Backing store. May be a block device (e.g. `/dev/sda`) that already has a FAT32 partition. |
| `WIIPI_TOKEN` | empty | If set, the web UI and management API need this token. Set it if the UI is reachable from outside your LAN. |
| `WIIPI_AUTO_INSTALL` | `1` | Install on its own while the Wii is not using the drive. |
| `WIIPI_IDLE_SECONDS` | `60` | How long the drive must be unused before an automatic install. |
| `WIIPI_PORT` | `8765` | HTTP port. |
| `WIIPI_UDC` | auto | USB device controller name from `/sys/class/udc`. |
| `WIIPI_STALL` | `0` | Mass-storage `stall` option. Try `1` if the Wii does not detect the drive. |
| `WIIPI_DETACH_MODE` | `unplug` | How the drive is taken from the Wii during an install. `unplug` drops off the USB bus; `eject` stays connected and only removes the medium. Try `eject` if the loader cannot find the drive again after a sync. |
| `WIIPI_FSCK` | `0` | Run `fsck.fat -a` on the volume before each install. |
| `WIIPI_WIT_ARGS` | empty | Extra arguments for `wit copy`, e.g. `--psel data` to drop update partitions. |
| `WIIPI_FAKE` | `0` | Development mode: no USB gadget, a plain folder as the drive. |

The loader endpoints (`/api/wii/*`) take no token, but only answer direct requests from
private addresses; requests that arrive through a tunnel or reverse proxy are refused.

## Loader protocol

Plain HTTP GET, answers are `key=value` lines.

- `GET /api/wii/status`: `state` (`attached`/`detached`), `busy`, `gen` (increments after every sync), `pending`, `preparing`, `done`/`total` (bytes), `message`, `error`.
- `GET /api/wii/release`: "the loader has unmounted the drive". Answers `accepted=1` and starts the sync if there is work. The loader then polls `status` until `gen` changes and `busy=0`.

## Troubleshooting

- **`no USB device controller found`**: the dwc2 overlay is not active. Run `host-setup.sh`, reboot, check `ls /sys/class/udc`.
- **The Wii does not see the drive**: check the cable carries data and is in the Pi's device-mode port; look at `cat /sys/class/udc/*/state` on the Pi (`configured` means the Wii is talking to it); try `WIIPI_STALL=1`.
- **Pi Sync cannot reach the Pi**: use the IP address, not a `.local` name; confirm the port is published; `curl http://<pi>:8765/api/wii/status` from another LAN machine should print `wiipi=1`.
- **The loader froze after a forced install**: with a cIOS the storage driver waits for the drive to return, so it resumes when the Pi re-attaches it. Prefer Pi Sync over forcing.
- **Reset the drive**: stop the container, delete `data/wii.img`, start it again.

## Development

```sh
pip install aiohttp pytest
python -m pytest tests            # includes a loader <-> Pi hand-off test (needs g++)
WIIPI_FAKE=1 WIIPI_DATA=/tmp/wiipi WIIPI_INBOX=/tmp/wiipi/inbox python -m wiipi
```
