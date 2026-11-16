"""Install queue and the drive hand-off state machine.

Only one side may use the drive at a time:

* While the drive is attached, the Wii owns it and the Pi never writes to it.
* To install, the Pi detaches the drive (the Wii sees it unplugged), mounts the
  image locally, copies the prepared games in, unmounts and re-attaches it.

A sync starts when one of these is true and there is work to do:

* the Wii is not using the drive (USB host not configured for ``idle_seconds``),
* USB Loader GX released the drive over the LAN (``/api/wii/release``),
* the user forced it from the web UI.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import discs
from .config import Config

log = logging.getLogger("wiipi.service")

HISTORY_LIMIT = 50
INBOX_SETTLE_SECONDS = 10.0
ERROR_BACKOFF_SECONDS = 60.0
GAME_ROOTS = ("wbfs", "games")
# Leave a little slack on the FAT volume for directory entries.
SPACE_MARGIN = 8 * 1024**2


@dataclass
class Item:
    id: int
    name: str
    status: str = "received"   # received | preparing | ready | installing | installed | failed
    source: str = ""
    game_id: str = ""
    title: str = ""
    kind: str = ""
    dest: str = ""
    files: list[str] = field(default_factory=list)
    size: int = 0
    error: str = ""
    created: float = 0.0
    finished: float = 0.0
    progress: float = 0.0


class Service:
    def __init__(self, cfg: Config, gadget, drive, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.gadget = gadget
        self.drive = drive
        self.clock = clock
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.items: list[Item] = []
        self.installed: list[dict] = []
        self.pending_deletes: list[str] = []
        self.gen = 0
        self.next_id = 1
        self.busy = False
        self.phase = "idle"
        self.error = ""
        self.message = ""
        self.done_bytes = 0
        self.total_bytes = 0
        self.free_bytes = 0
        self.size_bytes = 0
        self.wit = discs.have_wit()
        self._release = False
        self._force = False
        self._rescan = False
        self._retry_after = 0.0
        self._last_active = clock()
        self._inbox_seen: dict[str, tuple[int, float, float]] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        try:
            data = json.loads(self.cfg.state_file.read_text())
        except (OSError, ValueError):
            return
        self.gen = int(data.get("gen", 0))
        self.next_id = int(data.get("next_id", 1))
        self.installed = list(data.get("installed", []))
        self.pending_deletes = list(data.get("pending_deletes", []))
        self.free_bytes = int(data.get("free_bytes", 0))
        self.size_bytes = int(data.get("size_bytes", 0))
        known = Item.__dataclass_fields__.keys()
        for raw in data.get("items", []):
            item = Item(**{k: v for k, v in raw.items() if k in known})
            # Recover from a restart in the middle of work.
            if item.status == "preparing":
                item.status = "received"
            if item.status == "installing":
                item.status = "ready"
            if item.status == "received" and not Path(item.source).exists():
                item.status, item.error = "failed", "upload was lost in a restart"
            if item.status == "ready" and not (self.cfg.ready / str(item.id)).is_dir():
                item.status, item.error = "failed", "prepared files were lost in a restart"
            item.progress = 0.0
            self.items.append(item)

    def save(self) -> None:
        with self.lock:
            data = {
                "gen": self.gen,
                "next_id": self.next_id,
                "installed": self.installed,
                "pending_deletes": self.pending_deletes,
                "free_bytes": self.free_bytes,
                "size_bytes": self.size_bytes,
                "items": [asdict(item) for item in self.items],
            }
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.cfg.state_file)

    # -- startup ---------------------------------------------------------
    def initialise(self) -> None:
        for directory in (self.cfg.data_dir, self.cfg.uploads, self.cfg.ready):
            directory.mkdir(parents=True, exist_ok=True)
        self.load()
        if not self.drive.exists():
            self.drive.create(self.cfg.image_size)
        self.gadget.setup()
        if not self.gadget.attached:
            # Nobody can be using the drive yet: apply anything outstanding,
            # read the game list, then plug the drive in.
            self._sync("startup")
        else:
            log.info("drive is already attached; leaving it alone")

    def start(self) -> None:
        for target, name in ((self._prepare_loop, "prepare"), (self._sync_loop, "sync")):
            thread = threading.Thread(target=target, name=f"wiipi-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
        for thread in self._threads:
            thread.join(timeout=5)

    # -- queue -----------------------------------------------------------
    def add_file(self, path: Path, name: str) -> Item:
        with self.lock:
            item = Item(id=self.next_id, name=name, source=str(path), created=time.time())
            self.next_id += 1
            self.items.append(item)
            self._trim_history()
        self.save()
        self.wake.set()
        log.info("queued %s as item %d", name, item.id)
        return item

    def _trim_history(self) -> None:
        finished = [i for i in self.items if i.status in ("installed", "failed")]
        for item in finished[:-HISTORY_LIMIT]:
            self.items.remove(item)

    def cancel(self, item_id: int) -> bool:
        with self.lock:
            item = next((i for i in self.items if i.id == item_id), None)
            if item is None or item.status in ("preparing", "installing"):
                return False
            self.items.remove(item)
        if item.source:
            Path(item.source).unlink(missing_ok=True)
        shutil.rmtree(self.cfg.ready / str(item.id), ignore_errors=True)
        self.save()
        return True

    def request_delete(self, path: str) -> bool:
        """Queue removal of an installed game (applied at the next sync)."""
        with self.lock:
            if not any(game["path"] == path for game in self.installed):
                return False
            if path not in self.pending_deletes:
                self.pending_deletes.append(path)
        self.save()
        self.wake.set()
        return True

    def undo_delete(self, path: str) -> bool:
        with self.lock:
            if path not in self.pending_deletes:
                return False
            self.pending_deletes.remove(path)
        self.save()
        return True

    def request_sync(self, force: bool = False, rescan: bool = False) -> None:
        with self.lock:
            self._force = self._force or force
            self._rescan = self._rescan or rescan
            self._retry_after = 0.0
        self.wake.set()

    # -- preparing uploads -----------------------------------------------
    def scan_inbox(self) -> None:
        """Pick up files dropped into the inbox folder once they stop growing."""
        inbox = self.cfg.inbox
        if not inbox.is_dir():
            return
        now = self.clock()
        present = set()
        for path in sorted(inbox.iterdir()):
            if not path.is_file() or path.suffix.lower() not in discs.EXTENSIONS:
                continue
            present.add(path.name)
            stat = path.stat()
            seen = self._inbox_seen.get(path.name)
            if seen is None or seen[0] != stat.st_size or seen[1] != stat.st_mtime:
                self._inbox_seen[path.name] = (stat.st_size, stat.st_mtime, now)
                continue
            if now - seen[2] < INBOX_SETTLE_SECONDS:
                continue
            with self.lock:
                target = self.cfg.uploads / f"inbox-{self.next_id}{path.suffix.lower()}"
            try:
                shutil.move(str(path), str(target))
            except OSError as exc:
                log.warning("cannot take %s from the inbox: %s", path.name, exc)
                continue
            self._inbox_seen.pop(path.name, None)
            self.add_file(target, path.name)
        for name in list(self._inbox_seen):
            if name not in present:
                del self._inbox_seen[name]

    def prepare_step(self) -> bool:
        """Convert one received upload into its on-drive files. Returns True if it did work."""
        with self.lock:
            item = next((i for i in self.items if i.status == "received"), None)
            if item is None:
                return False
            item.status = "preparing"
        work_dir = self.cfg.ready / str(item.id)

        def progress(done: int, total: int) -> None:
            item.progress = done / total if total else 0.0

        try:
            prepared = discs.prepare(Path(item.source), work_dir, progress, self.cfg.wit_args)
        except Exception as exc:  # noqa: BLE001 - report anything to the user
            log.warning("item %d (%s) failed to prepare: %s", item.id, item.name, exc)
            shutil.rmtree(work_dir, ignore_errors=True)
            Path(item.source).unlink(missing_ok=True)
            with self.lock:
                item.status, item.error, item.finished = "failed", str(exc), time.time()
        else:
            with self.lock:
                item.game_id = prepared.info.game_id
                item.title = prepared.info.title
                item.kind = prepared.info.kind
                item.dest = prepared.info.dest_dir
                item.files = prepared.files
                item.size = prepared.size
                item.source = ""
                item.progress = 0.0
                item.status = "ready"
            log.info("item %d ready: %s", item.id, item.dest)
        self.save()
        self.wake.set()
        return True

    def _prepare_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_inbox()
                if self.prepare_step():
                    continue
            except Exception:  # noqa: BLE001
                log.exception("prepare loop error")
            self._stop.wait(2.0)

    # -- syncing ---------------------------------------------------------
    def host_active(self) -> bool:
        """True while a USB host (the Wii) has the drive configured."""
        state = self.gadget.host_state()
        # Anything we cannot positively identify as idle counts as in use.
        return state in ("configured", "unknown")

    def host_idle(self) -> bool:
        if self.host_active():
            self._last_active = self.clock()
            return False
        return self.clock() - self._last_active >= self.cfg.idle_seconds

    def _work(self) -> tuple[list[Item], list[str]]:
        return [i for i in self.items if i.status == "ready"], list(self.pending_deletes)

    def sync_step(self) -> bool:
        """Run a sync if one is due. Returns True if a sync ran."""
        idle = self.host_idle()
        with self.lock:
            ready, deletes = self._work()
            has_work = bool(ready or deletes or self._rescan)
            if not has_work:
                self._release = self._force = False
                return False
            if self._release:
                reason = "released by USB Loader GX"
            elif self._force:
                reason = "forced from the web UI"
            elif self.cfg.auto_install and idle and self.clock() >= self._retry_after:
                reason = "the Wii is not using the drive"
            else:
                return False
        self._sync(reason)
        return True

    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_step()
            except Exception:  # noqa: BLE001
                log.exception("sync loop error")
            self.wake.wait(1.0)
            self.wake.clear()

    def _set_phase(self, phase: str, message: str = "") -> None:
        self.phase, self.message = phase, message

    def _sync(self, reason: str) -> None:
        with self.lock:
            items, deletes = self._work()
            for item in items:
                item.status = "installing"
            self.busy = True
            self.error = ""
            self.done_bytes = 0
            self.total_bytes = sum(item.size for item in items)
        log.info("sync starting (%s): %d install(s), %d removal(s)", reason, len(items), len(deletes))
        failure = ""
        try:
            self._set_phase("detaching", "Detaching the drive")
            self.gadget.detach()
            self._set_phase("mounting", "Opening the drive")
            with self.drive.mounted() as root:
                for path in deletes:
                    self._set_phase("removing", f"Removing {Path(path).name}")
                    self._remove(root, path)
                for item in items:
                    self._set_phase("copying", f"Copying {item.title or item.name}")
                    self._install(root, item)
                self._set_phase("scanning", "Reading the game list")
                listing = scan_games(root)
                usage = shutil.disk_usage(root)
                with self.lock:
                    self.installed = listing
                    self.free_bytes, self.size_bytes = usage.free, usage.total
                    self.pending_deletes = [p for p in self.pending_deletes if p not in deletes]
                self._set_phase("unmounting", "Finishing up")
        except Exception as exc:  # noqa: BLE001
            failure = str(exc)
            log.exception("sync failed")
        finally:
            if getattr(self.drive, "is_mounted", False):
                # Never let the Wii and the Pi have the volume open together.
                failure = "the drive could not be unmounted on the Pi, so it was left detached"
                log.error(failure)
            else:
                try:
                    self._set_phase("attaching", "Re-attaching the drive")
                    self.gadget.attach()
                except Exception as exc:  # noqa: BLE001
                    failure = failure or f"could not re-attach the drive: {exc}"
                    log.exception("re-attach failed")
            with self.lock:
                for item in items:
                    if item.status == "installing":  # sync died before reaching it
                        item.status, item.progress = "ready", 0.0
                self.error = failure
                self._retry_after = self.clock() + ERROR_BACKOFF_SECONDS if failure else 0.0
                self._release = self._force = self._rescan = False
                self._last_active = self.clock()
                self.gen += 1
                self.busy = False
                self._set_phase("idle")
            self.save()
        log.info("sync finished%s", f" with error: {failure}" if failure else "")

    @staticmethod
    def _resolve(root: Path, rel: str) -> Path:
        """Map a drive-relative game path to a real path, refusing anything outside the game folders."""
        parts = Path(rel).parts
        if len(parts) != 2 or parts[0] not in GAME_ROOTS or parts[1] in ("", ".", ".."):
            raise ValueError(f"refusing to touch {rel!r}")
        return root / parts[0] / parts[1]

    def _remove(self, root: Path, rel: str) -> None:
        target = self._resolve(root, rel)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            # A loose image may have split parts next to it (ID.wbf1, ID.wbf2 ...).
            for part in target.parent.glob(f"{glob_escape(target.stem)}.wbf[0-9]"):
                part.unlink()
            target.unlink()
        log.info("removed %s", rel)

    def _install(self, root: Path, item: Item) -> None:
        source_dir = self.cfg.ready / str(item.id)
        base = self.done_bytes
        try:
            dest = self._resolve(root, item.dest)
            reclaim = tree_size(dest) if dest.exists() else 0
            free = shutil.disk_usage(root).free + reclaim
            if item.size + SPACE_MARGIN > free:
                raise discs.DiscError(
                    f"not enough space on the drive ({item.size // 1024**2} MiB needed, "
                    f"{free // 1024**2} MiB free)")
            if dest.exists():
                shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
            dest.mkdir(parents=True)

            def progress(done: int, _total: int) -> None:
                self.done_bytes = done
                item.progress = (done - base) / item.size if item.size else 1.0

            offset = base
            for name in item.files:
                offset += discs.copy_file(source_dir / name, dest / name, progress, base=offset,
                                          total=self.total_bytes, sync=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("item %d (%s) failed to install: %s", item.id, item.name, exc)
            try:
                shutil.rmtree(self._resolve(root, item.dest), ignore_errors=True)
            except ValueError:
                pass
            with self.lock:
                item.status, item.error, item.finished = "failed", str(exc), time.time()
            self.done_bytes = base + item.size
        else:
            with self.lock:
                item.status, item.progress, item.finished = "installed", 1.0, time.time()
            self.done_bytes = base + item.size
            log.info("installed %s", item.dest)
        shutil.rmtree(source_dir, ignore_errors=True)

    # -- the Wii side of the hand-off --------------------------------------
    def wii_release(self) -> bool:
        """USB Loader GX has unmounted the drive and is waiting for us."""
        with self.lock:
            if self.busy:
                return True
            ready, deletes = self._work()
            if not (ready or deletes or self._rescan):
                return False
            self._release = True
        self.wake.set()
        return True

    def wii_status(self) -> str:
        with self.lock:
            ready, deletes = self._work()
            preparing = sum(1 for i in self.items if i.status in ("received", "preparing"))
            newest = max([i.id for i in ready], default=0)
            fields = {
                "wiipi": 1,
                "state": "attached" if self.gadget.attached else "detached",
                "busy": int(self.busy or self._release),
                "gen": self.gen,
                "pending": len(ready) + len(deletes),
                "preparing": preparing,
                "newest": newest + len(deletes),
                "done": self.done_bytes if self.busy else 0,
                "total": self.total_bytes if self.busy else 0,
                "message": self.message,
                "error": self.error.replace("\n", " "),
            }
        return "".join(f"{key}={value}\n" for key, value in fields.items())

    def status(self) -> dict:
        host_state = self.gadget.host_state()
        with self.lock:
            return {
                "drive": {
                    "attached": self.gadget.attached,
                    "host_state": host_state,
                    "host_active": host_state in ("configured", "unknown"),
                    "busy": self.busy,
                    "phase": self.phase,
                    "message": self.message,
                    "error": self.error,
                    "free": self.free_bytes,
                    "size": self.size_bytes,
                    "backing": str(self.drive.backing),
                },
                "gen": self.gen,
                "progress": {"done": self.done_bytes, "total": self.total_bytes},
                "items": [
                    {k: v for k, v in asdict(item).items() if k != "source"}
                    for item in reversed(self.items)
                ],
                "installed": self.installed,
                "pending_deletes": list(self.pending_deletes),
                "auto_install": self.cfg.auto_install,
                "idle_seconds": self.cfg.idle_seconds,
                "wit": self.wit,
            }


def glob_escape(text: str) -> str:
    return "".join(f"[{ch}]" if ch in "*?[" else ch for ch in text)


def tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def scan_games(root: Path) -> list[dict]:
    """List the games on the drive (one entry per folder or loose image file)."""
    games: list[dict] = []
    for top, kind in (("wbfs", "wii"), ("games", "gc")):
        base = root / top
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix.lower() not in (".wbfs", ".iso", ".ciso"):
                continue  # split parts and stray files belong to another entry
            name = entry.name if entry.is_dir() else entry.stem
            game_id, title = "", name
            if "[" in name and name.rstrip().endswith("]"):
                title, _, tail = name.rpartition("[")
                game_id, title = tail.rstrip("] "), title.strip()
            elif "_" in name and len(name.split("_", 1)[0]) in (4, 6):
                game_id, title = name.split("_", 1)
            elif len(name) in (4, 6) and name.isalnum():
                game_id = name
            games.append({
                "path": f"{top}/{entry.name}",
                "title": title or name,
                "id": game_id,
                "kind": kind,
                "size": tree_size(entry),
            })
    return games
