import json

from conftest import make_iso, make_wbfs
from wiipi import discs
from wiipi.service import Service, scan_games


def queue(env, name="up.wbfs", game_id="RSPE01", title="Wii Sports"):
    path = make_wbfs(env.cfg.uploads / name, game_id, title)
    item = env.service.add_file(path, name)
    assert env.service.prepare_step()
    return item


def wii(env) -> dict:
    return dict(line.split("=", 1) for line in env.service.wii_status().splitlines())


def test_startup_attaches_the_drive(env):
    assert env.gadget.events == ["setup", "detach", "attach"]
    assert env.gadget.attached and env.service.gen == 1
    assert (env.drive.root / "wbfs").is_dir()


def test_installs_when_wii_is_off(env):
    item = queue(env)
    assert item.status == "ready" and item.dest == "wbfs/Wii Sports [RSPE01]"
    env.gadget.state = "not attached"
    assert not env.service.sync_step()          # must first be idle for idle_seconds
    env.clock.now += 61
    assert env.service.sync_step()
    game = env.drive.root / "wbfs" / "Wii Sports [RSPE01]" / "RSPE01.wbfs"
    assert game.stat().st_size == 8192
    assert item.status == "installed" and env.gadget.attached
    assert env.service.installed[0]["id"] == "RSPE01"
    assert not (env.cfg.ready / str(item.id)).exists()  # staging cleaned up


def test_waits_while_wii_uses_the_drive(env):
    queue(env)
    env.gadget.state = "configured"
    for _ in range(5):
        env.clock.now += 600
        assert not env.service.sync_step()
    assert env.gadget.events.count("detach") == 1  # only the startup one
    status = wii(env)
    assert status["pending"] == "1" and status["busy"] == "0" and status["state"] == "attached"


def test_unknown_host_state_counts_as_in_use(env):
    queue(env)
    env.gadget.state = "unknown"
    env.clock.now += 600
    assert not env.service.sync_step()


def test_release_from_loader(env):
    item = queue(env)
    env.gadget.state = "configured"
    gen = int(wii(env)["gen"])
    assert env.service.wii_release()
    assert wii(env)["busy"] == "1"              # loader sees busy before the sync thread runs
    assert env.service.sync_step()
    status = wii(env)
    assert int(status["gen"]) == gen + 1 and status["busy"] == "0" and status["pending"] == "0"
    assert item.status == "installed"
    # The drive was detached for the copy and is back afterwards.
    assert env.gadget.events[-2:] == ["detach", "attach"] and not env.drive.is_mounted


def test_release_with_nothing_to_do(env):
    env.gadget.state = "configured"
    assert not env.service.wii_release()
    assert not env.service.sync_step()
    assert wii(env)["busy"] == "0"


def test_idle_timer_restarts_after_activity(env):
    queue(env)
    env.gadget.state = "not attached"
    env.clock.now += 50
    assert not env.service.sync_step()
    env.gadget.state = "configured"             # Wii switched on
    assert not env.service.sync_step()
    env.gadget.state = "not attached"
    env.clock.now += 50
    assert not env.service.sync_step()          # only 50s idle since it was last active
    env.clock.now += 11
    assert env.service.sync_step()


def test_delete_and_replace(env):
    env.gadget.state = "configured"
    queue(env)
    env.service.wii_release()
    env.service.sync_step()
    path = "wbfs/Wii Sports [RSPE01]"
    assert not env.service.request_delete("wbfs/../../etc")
    assert env.service.request_delete(path)
    assert wii(env)["pending"] == "1"
    queue(env, "smg.wbfs", "RMGE01", "Super Mario Galaxy")
    assert env.service.wii_release() and env.service.sync_step()
    assert not (env.drive.root / path).exists()
    assert [g["id"] for g in env.service.installed] == ["RMGE01"]
    assert env.service.pending_deletes == []


def test_refuses_paths_outside_game_folders(env):
    for bad in ("wbfs", "wbfs/a/b", "apps/usbloader_gx", "wbfs/..", "../wbfs/x", "/etc/passwd"):
        try:
            Service._resolve(env.drive.root, bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} was accepted")


def test_not_enough_space_fails_cleanly(env, monkeypatch):
    item = queue(env)
    item.size = 10**15
    env.gadget.state = "configured"
    env.service.wii_release()
    env.service.sync_step()
    assert item.status == "failed" and "space" in item.error
    assert not (env.drive.root / item.dest).exists()
    assert env.gadget.attached and wii(env)["busy"] == "0"


def test_drive_always_comes_back_after_a_failed_sync(env):
    item = queue(env)
    env.gadget.state = "configured"
    env.gadget.fail_detach = True
    gen = env.service.gen
    env.service.wii_release()
    env.service.sync_step()
    assert env.gadget.attached                  # never left unplugged
    assert item.status == "ready"               # will be retried
    assert env.service.gen == gen + 1           # loader stops waiting and remounts
    assert "detach" in env.service.error
    assert env.drive.mount_count == 1           # only the startup mount: never mounted while attached
    # Auto mode backs off instead of hammering the drive.
    env.gadget.fail_detach = False
    env.gadget.state = "not attached"
    env.clock.now += 30
    env.service.sync_step()
    env.clock.now += 61
    assert env.service.sync_step() and item.status == "installed"


def test_stays_detached_if_unmount_fails(env):
    queue(env)
    env.gadget.state = "configured"

    class Stuck:
        backing = env.drive.root
        is_mounted = False

        def mounted(self):
            import contextlib

            @contextlib.contextmanager
            def cm():
                self.is_mounted = True
                yield env.drive.root
                raise RuntimeError("umount: target is busy")
            return cm()

    env.service.drive = Stuck()
    env.service.wii_release()
    env.service.sync_step()
    assert not env.gadget.attached              # both sides must never have it open
    assert "unmounted" in env.service.error


def test_inbox_pickup(env):
    make_wbfs(env.cfg.inbox / "Sports.wbfs")
    (env.cfg.inbox / "notes.txt").write_text("ignore me")
    env.service.scan_inbox()
    assert not env.service.items               # still settling
    env.clock.now += 11
    env.service.scan_inbox()
    assert [i.name for i in env.service.items] == ["Sports.wbfs"]
    assert not (env.cfg.inbox / "Sports.wbfs").exists() and (env.cfg.inbox / "notes.txt").exists()
    assert env.service.prepare_step() and env.service.items[0].status == "ready"


def test_bad_upload_fails_and_is_removed(env):
    bad = env.cfg.uploads / "bad.wbfs"
    bad.write_bytes(b"not a disc" * 100)
    item = env.service.add_file(bad, "bad.wbfs")
    env.service.prepare_step()
    assert item.status == "failed" and not bad.exists()
    assert wii(env)["pending"] == "0"


def test_state_survives_restart(env):
    item = queue(env)
    env.service.request_sync()
    state = json.loads(env.cfg.state_file.read_text())
    assert state["items"][0]["status"] == "ready"
    again = Service(env.cfg, env.gadget, env.drive, clock=env.clock)
    again.initialise()                          # drive is attached: must not touch it
    assert env.gadget.events.count("detach") == 1
    assert again.items[0].status == "ready" and again.items[0].id == item.id
    assert again.next_id == item.id + 1


def test_cancel(env):
    item = queue(env)
    assert env.service.cancel(item.id)
    assert not env.service.items and not (env.cfg.ready / str(item.id)).exists()
    assert not env.service.cancel(999)


def test_gamecube_goes_to_games_folder(env):
    path = make_iso(env.cfg.uploads / "melee.iso", "GALE01", "Melee", kind="gc")
    item = env.service.add_file(path, "melee.iso")
    env.service.prepare_step()
    env.gadget.state = "configured"
    env.service.wii_release()
    env.service.sync_step()
    assert (env.drive.root / "games" / "Melee [GALE01]" / "game.iso").exists()
    assert item.kind == "gc" and env.service.installed[0]["kind"] == "gc"


def test_scan_understands_loader_layouts(tmp_path):
    wbfs = tmp_path / "wbfs"
    (wbfs / "Wii Sports [RSPE01]").mkdir(parents=True)
    (wbfs / "Wii Sports [RSPE01]" / "RSPE01.wbfs").write_bytes(b"x" * 10)
    (wbfs / "RMGE01_Super Mario Galaxy").mkdir()
    (wbfs / "RZDE01.wbfs").write_bytes(b"x" * 5)
    (wbfs / "RZDE01.wbf1").write_bytes(b"x" * 5)
    games = {g["path"]: g for g in scan_games(tmp_path)}
    assert games["wbfs/Wii Sports [RSPE01]"]["id"] == "RSPE01"
    assert games["wbfs/Wii Sports [RSPE01]"]["size"] == 10
    assert games["wbfs/RMGE01_Super Mario Galaxy"]["title"] == "Super Mario Galaxy"
    assert games["wbfs/RZDE01.wbfs"]["id"] == "RZDE01"
    assert "wbfs/RZDE01.wbf1" not in games


def test_split_game_installs_all_parts(env, monkeypatch):
    monkeypatch.setattr(discs, "SPLIT_SIZE", 4096)
    orig = discs.split_into
    monkeypatch.setattr(discs, "split_into",
                        lambda src, dest, base, progress=None, split_size=4096, move=False:
                        orig(src, dest, base, progress, 4096, move))
    item = queue(env)
    assert item.files == ["RSPE01.wbfs", "RSPE01.wbf1"]
    env.gadget.state = "configured"
    env.service.wii_release()
    env.service.sync_step()
    folder = env.drive.root / "wbfs" / "Wii Sports [RSPE01]"
    assert sorted(p.name for p in folder.iterdir()) == ["RSPE01.wbf1", "RSPE01.wbfs"]
    assert env.service.done_bytes == env.service.total_bytes == 8192
