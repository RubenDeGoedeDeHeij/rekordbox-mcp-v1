import hashlib
import json
from pathlib import Path

import pytest

from rekordbox_mcp import safety, server
from rekordbox_mcp.config import get_config
from rekordbox_mcp.db import open_db
from rekordbox_mcp.library import read_file_genre


def db_hash() -> str:
    cfg = get_config()
    h = hashlib.sha256(cfg.db_path.read_bytes())
    wal = cfg.db_path.with_name(cfg.db_path.name + "-wal")
    if wal.exists():
        h.update(wal.read_bytes())
    return h.hexdigest()


def backups():
    return safety.list_backups()


def log_entries():
    return safety.read_action_log(1000)


# -------------------------------------------------------------------------- safety


def test_backup_now_creates_verified_copy(lib):
    m = server.backup_now("t")
    p = Path(m["backup_path"])
    assert (p / "master.db").stat().st_size > 0
    assert hashlib.sha256((p / "master.db").read_bytes()).hexdigest() == m["master_db_sha256"]
    assert (p / "share/PIONEER/USBANLZ").is_dir() and m["anlz"]["files"] == 2
    assert (p / "MYSETTING.DAT").exists() and (p / "backup_manifest.json").exists()


def test_write_refused_while_rekordbox_runs(lib, monkeypatch):
    monkeypatch.setattr(safety, "find_rekordbox_processes", lambda: [{"pid": 4242, "name": "rekordbox"}])
    before = db_hash()
    with pytest.raises(safety.RekordboxRunningError):
        server.create_playlist("X", dry_run=False)
    assert db_hash() == before
    assert backups() == []


def test_dry_run_writes_nothing(lib):
    before = db_hash()
    res = server.create_playlist("Dry", parent="Sets", dry_run=True)
    assert res["dry_run"] and res["plan"]["also_create_folders"] == ["Sets"]
    assert db_hash() == before and backups() == []
    assert log_entries()[-1]["status"] == "DRY-RUN"


# ------------------------------------------------------------------------ playlists


def test_playlist_lifecycle(lib):
    res = server.create_playlist("Friday", parent="Sets/2026", dry_run=False)
    assert Path(res["backup_path"]).exists()
    paths = {p["path"] for p in server.list_playlists()}
    assert {"Sets", "Sets/2026", "Sets/2026/Friday"} <= paths

    server.add_tracks_to_playlist("Sets/2026/Friday", ["Demo Track 2", "FX - NOISE", "178162577"], dry_run=False)
    tracks = server.get_playlist_tracks("Sets/2026/Friday")["tracks"]
    assert [t["title"] for t in tracks] == ["Demo Track 2", "NOISE", "Demo Track 1"]

    server.reorder_playlist("Sets/2026/Friday", sort_by="bpm_ascending", dry_run=False)
    tracks = server.get_playlist_tracks("Sets/2026/Friday")["tracks"]
    assert [t["bpm"] for t in tracks] == [120.0, 128.0, 132.0]
    assert [t["position"] for t in tracks] == [1, 2, 3]

    server.remove_tracks_from_playlist("Sets/2026/Friday", ["NOISE"], dry_run=False)
    assert len(server.get_playlist_tracks("Sets/2026/Friday")["tracks"]) == 2

    server.delete_playlist("Sets/2026/Friday", dry_run=False)
    assert "Sets/2026/Friday" not in {p["path"] for p in server.list_playlists()}
    ok = [e for e in log_entries() if e["status"] == "OK"]
    assert len(ok) == 5 and all(e["backup_path"] for e in ok)
    assert len(backups()) == 5


def test_build_set(lib):
    res = server.build_set_from_criteria("Peak", bpm_min=125, bpm_max=135, ordering="energy_ramp", dry_run=False)
    tracks = server.get_playlist_tracks("Sets/Peak")["tracks"]
    energies = [t["energy"] for t in tracks if t["energy"] is not None]
    assert energies == sorted(energies) and len(tracks) == res["plan"]["track_count"]
    assert all(t["exists"] for t in tracks)


def test_build_set_from_names_keeps_order(lib):
    names = ["FX - SIREN", "Demo Track 2", "HORN"]
    server.build_set_from_criteria("Named", track_names=names, parent_folder=None, dry_run=False)
    assert [t["title"] for t in server.get_playlist_tracks("Named")["tracks"]] == ["SIREN", "Demo Track 2", "HORN"]


# -------------------------------------------------------------------------- library


def test_set_genre_writes_rekordbox_and_id3(lib):
    server.set_track_genre(["FX - HORN"], "Minimal", dry_run=False)
    t = server.get_track("FX - HORN")
    assert t["genre"] == "Minimal"
    assert read_file_genre(t["path"]) == "Minimal"


def test_organize_moves_files(lib):
    server.organize_library_by_genre(assignments={"FX - SIREN": "Tech House"}, move_files=True, dry_run=False)
    t = server.get_track("FX - SIREN")
    assert t["exists"] and Path(t["path"]).parent.name == "Tech House"
    assert "02 Library" in t["path"]


def test_dedupe_never_keeps_dead_reference(lib):
    report = server.dedupe_library(action="report")
    grp2 = [g for g in report["report"] if any("Demo Track 2" in t["track"] for t in g["tracks"])][0]
    keeper = [t for t in grp2["tracks"] if t["keeper"]][0]
    assert keeper["exists"]  # the dead reference (has a playlist entry) must not win

    res = server.dedupe_library(action="remove", dry_run=False)
    assert res["result"]["merged"]
    with open_db() as db:
        titles = [c.Title for c in db.get_content().all()]
    assert titles.count("Demo Track 1") == 1 and titles.count("Demo Track 2") == 1
    # playlist entries of removed duplicates now point at the keepers
    warm = server.get_playlist_tracks("Test Folder/Warmup")["tracks"]
    assert sorted(t["id"] for t in warm) == ["178162577", "66382436"]
    moved = res["result"]["files_moved_to_trash"]
    assert len(moved) == 1 and Path(moved[0][1]).exists()  # the (copy).mp3; dead ref has no file


# ------------------------------------------------------------------------- settings


def test_settings_roundtrip(lib):
    s = server.get_settings()
    assert s["MYSETTING.DAT"]["values"]["quantize"] in ("on", "off")
    new = "off" if s["MYSETTING.DAT"]["values"]["quantize"] == "on" else "on"
    res = server.update_setting("quantize", new, dry_run=False)
    assert res["result"]["verified_value"] == new
    assert server.get_settings("MYSETTING")["MYSETTING.DAT"]["values"]["quantize"] == new
    with pytest.raises(ValueError):
        server.update_setting("quantize", "maybe", dry_run=False)


# ----------------------------------------------------------------- cues & beatgrids


def test_ambiguous_name_is_refused(lib):
    # two existing copies of "Demo Track 1": never guess which one to write to
    with pytest.raises(Exception, match="niet eenduidig"):
        server.write_cue_points("Demo Track 1", [{"type": "memory", "time": 1}], mode="execute", force=True)


def test_cues_dry_run_then_execute(lib):
    cues = [{"type": "memory", "time": "0:10"}, {"type": "hot", "slot": "C", "time": 20, "loop_end": 24,
                                                 "color": "red", "comment": "loop"}]
    before = db_hash()
    dry = server.write_cue_points("178162577", cues)
    assert dry["dry_run"] and db_hash() == before
    # no hot cues in this library to confirm the Kind mapping -> refused without force
    with pytest.raises(Exception):
        server.write_cue_points("178162577", cues, mode="execute")
    assert db_hash() == before
    server.write_cue_points("178162577", cues, mode="execute", force=True)
    got = server.inspect_track_cues("178162577")["db_cues_djmdCue"]
    assert [(c["type"], c["time_sec"], c["loop_end_sec"]) for c in got] == [("memory", 10.0, None), ("hot", 20.0, 24.0)]
    assert got[1]["slot_assumed"] == "C"
    # conflicting slot without replace_existing is refused
    with pytest.raises(Exception):
        server.write_cue_points("178162577", [{"type": "hot", "slot": "C", "time": 30}], mode="execute", force=True)


def test_beatgrid(lib):
    info = server.inspect_track_cues("178162577")["beatgrid"]
    assert info["beats"] > 0
    dry = server.write_beatgrid("178162577", 128.0, "0.25")
    assert dry["dry_run"]
    server.write_beatgrid("178162577", 124.5, "0:00.300", mode="execute")
    after = server.inspect_track_cues("178162577")["beatgrid"]
    assert after["beats"] == info["beats"]
    assert abs(after["first_beat_sec"] - 0.3) < 0.002 and after["bpm_unique"] == [124.5]
    assert server.get_track("178162577")["bpm"] == 124.5


def test_restore_backup(lib):
    server.create_playlist("Temp", dry_run=False)
    first_backup = backups()[0]["backup_path"]  # state before "Temp" existed
    assert "Temp" in {p["name"] for p in server.list_playlists()}
    server.restore_backup(first_backup, dry_run=False)
    assert "Temp" not in {p["name"] for p in server.list_playlists()}
    entries = json.loads(json.dumps(log_entries()))
    assert entries[-1]["action"] == "restore_backup"
