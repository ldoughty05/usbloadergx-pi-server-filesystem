import pytest

from conftest import make_iso, make_wbfs
from wiipi import discs


def test_identify_wii_iso(tmp_path):
    info = discs.identify(make_iso(tmp_path / "a.iso"))
    assert (info.kind, info.container, info.game_id, info.title) == ("wii", "iso", "RMGE01", "SUPER MARIO GALAXY")
    assert info.dest_dir == "wbfs/SUPER MARIO GALAXY [RMGE01]"


def test_identify_wbfs_and_gc(tmp_path):
    info = discs.identify(make_wbfs(tmp_path / "a.wbfs"))
    assert (info.kind, info.container, info.game_id) == ("wii", "wbfs", "RSPE01")
    gc = discs.identify(make_iso(tmp_path / "b.iso", "GALE01", "Super Smash Bros Melee", kind="gc"))
    assert gc.kind == "gc" and gc.dest_dir == "games/Super Smash Bros Melee [GALE01]"


def test_identify_rejects_junk(tmp_path):
    junk = tmp_path / "junk.iso"
    junk.write_bytes(b"\0" * 4096)
    with pytest.raises(discs.DiscError):
        discs.identify(junk)
    tiny = tmp_path / "tiny.iso"
    tiny.write_bytes(b"abc")
    with pytest.raises(discs.DiscError):
        discs.identify(tiny)


def test_clean_title():
    assert discs.clean_title('Mario & Sonic: At the "Games"?') == "Mario & Sonic At the Games"
    assert discs.clean_title("Pokémon Battle...  ") == "Pokmon Battle"
    assert discs.clean_title("///") == "Game"


def test_split_matches_loader_naming(tmp_path):
    src = tmp_path / "big.wbfs"
    payload = bytes(range(256)) * 40  # 10240 bytes
    src.write_bytes(payload)
    seen = []
    names = discs.split_into(src, tmp_path / "out", "RMGE01", lambda d, t: seen.append((d, t)),
                             split_size=4096, move=True)
    assert names == ["RMGE01.wbfs", "RMGE01.wbf1", "RMGE01.wbf2"]
    assert [(tmp_path / "out" / n).stat().st_size for n in names] == [4096, 4096, 2048]
    assert b"".join((tmp_path / "out" / n).read_bytes() for n in names) == payload
    assert not src.exists()
    assert seen[-1] == (len(payload), len(payload))


def test_split_size_is_one_cluster_under_4g():
    assert discs.SPLIT_SIZE == 4 * 1024**3 - 32 * 1024


def test_prepare_wbfs_and_gc(tmp_path):
    prepared = discs.prepare(make_wbfs(tmp_path / "up.wbfs"), tmp_path / "w1")
    assert prepared.files == ["RSPE01.wbfs"] and prepared.size == 8192
    assert not (tmp_path / "up.wbfs").exists()

    gc = discs.prepare(make_iso(tmp_path / "gc.iso", "GALE01", "Melee", kind="gc", disc=1), tmp_path / "w2")
    assert gc.files == ["disc2.iso"] and gc.info.dest_dir == "games/Melee [GALE01]"


def test_prepare_iso_uses_wit(tmp_path, monkeypatch):
    calls = []

    def fake_convert(src, dst, extra=""):
        calls.append((src.name, extra))
        make_wbfs(dst, "RMGE01", "SUPER MARIO GALAXY")

    monkeypatch.setattr(discs, "have_wit", lambda: True)
    monkeypatch.setattr(discs, "wit_convert", fake_convert)
    prepared = discs.prepare(make_iso(tmp_path / "smg.iso"), tmp_path / "work", wit_args="--psel data")
    assert calls == [("smg.iso", "--psel data")]
    assert prepared.files == ["RMGE01.wbfs"]
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == ["RMGE01.wbfs"]
    assert not (tmp_path / "smg.iso").exists()


def test_prepare_iso_without_wit(tmp_path, monkeypatch):
    monkeypatch.setattr(discs, "have_wit", lambda: False)
    with pytest.raises(discs.DiscError, match="wit"):
        discs.prepare(make_iso(tmp_path / "smg.iso"), tmp_path / "work")
