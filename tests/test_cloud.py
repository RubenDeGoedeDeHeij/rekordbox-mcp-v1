"""Cloud Library Sync (soft delete) and Google Drive / Dropbox (online-only files)."""

from pathlib import Path

import pytest

from rekordbox_mcp import cloud, server
from rekordbox_mcp.config import get_config
from rekordbox_mcp.db import open_db, tables
from rekordbox_mcp.library import read_file_genre


@pytest.fixture
def cloud_lib(lib):
    """The test library, marked as synced with Rekordbox Cloud Library Sync."""
    with open_db() as db:
        for i, c in enumerate(db.get_content().all(), 1):
            c.rb_local_synced = 1
            c.usn = 1000 + i
        for p in db.get_playlist().all():
            if p.Attribute >= 0:
                p.rb_local_synced = 1
        db.commit()
    return lib


def _row(cls, id_):
    with open_db() as db:
        r = db.query(cls).filter_by(ID=str(id_)).first()
        return None if r is None else {"deleted": r.rb_local_deleted, "usn": r.rb_local_usn}


def test_status_without_sync_uses_hard_delete(lib):
    st = server.get_cloud_status()
    assert st["sync_active"] is False
    assert st["delete_strategy"]["djmdPlaylist"]["mode"] == "hard"


def test_status_detects_sync(cloud_lib):
    st = server.get_cloud_status()
    assert st["sync_active"] is True
    assert st["delete_strategy"]["djmdContent"]["mode"] == "soft"
    assert server.get_status()["cloud_library_sync_active"] is True


def test_delete_playlist_is_soft_with_sync(cloud_lib):
    server.create_playlist("Gone", parent="Sets", dry_run=False)
    pid = [p for p in server.list_playlists() if p["path"] == "Sets/Gone"][0]["id"]
    before = _row(tables.DjmdPlaylist, pid)
    dry = server.delete_playlist("Sets/Gone")
    assert dry["plan"]["delete_mode"].startswith("soft") and "cloud_sync_note" in dry["plan"]
    res = server.delete_playlist("Sets/Gone", dry_run=False)
    assert res["result"]["delete_mode"] == "soft"
    after = _row(tables.DjmdPlaylist, pid)
    assert after is not None and after["deleted"] == 1  # row kept for the sync, flagged deleted
    assert after["usn"] > before["usn"]  # change is visible to the sync
    assert "Sets/Gone" not in {p["path"] for p in server.list_playlists()}


def test_remove_track_soft_renumbers(cloud_lib):
    server.build_set_from_criteria("S", track_names=["HORN", "SIREN", "NOISE"], parent_folder=None, dry_run=False)
    server.remove_tracks_from_playlist("S", ["SIREN"], dry_run=False)
    tracks = server.get_playlist_tracks("S")["tracks"]
    assert [t["title"] for t in tracks] == ["HORN", "NOISE"] and [t["position"] for t in tracks] == [1, 2]


def test_dedupe_soft_keeps_rows_flagged(cloud_lib):
    res = server.dedupe_library(action="remove", dry_run=False)
    assert res["result"]["delete_mode"] == "soft"
    with open_db() as db:
        rows = [c for c in db.get_content().all() if c.Title == "Demo Track 1"]
    assert sorted(c.rb_local_deleted for c in rows) == [0, 1]
    assert server.search_tracks("Demo Track 1")["count"] == 1
    warm = server.get_playlist_tracks("Test Folder/Warmup")["tracks"]
    assert sorted(t["id"] for t in warm) == ["178162577", "66382436"]


def test_force_hard_delete_via_env(cloud_lib, monkeypatch):
    monkeypatch.setenv("RBMCP_DELETE_MODE", "hard")
    server.create_playlist("Tmp", dry_run=False)
    pid = [p for p in server.list_playlists() if p["path"] == "Tmp"][0]["id"]
    server.delete_playlist("Tmp", dry_run=False)
    assert _row(tables.DjmdPlaylist, pid) is None


# ------------------------------------------------------------------ online-only files


@pytest.fixture
def drive(lib, monkeypatch):
    """Pretend the export folder is Google Drive and HORN is a streamed placeholder."""
    music = lib / "lib" / "Music" / "DJ" / "03 Rekordbox Export"
    monkeypatch.setenv("RBMCP_CLOUD_ROOTS", str(music))
    real = cloud.file_state
    monkeypatch.setattr(cloud, "file_state", lambda p: "online_only" if p and "HORN" in p else real(p))
    return music


def test_online_only_not_tagged(drive):
    res = server.set_track_genre(["FX - HORN"], "Minimal", dry_run=False)
    t = server.get_track("FX - HORN")
    assert t["genre"] == "Minimal" and t["file_state"] == "online_only" and t["in_cloud_storage"]
    assert read_file_genre(t["path"]) != "Minimal"  # file not touched
    assert "online" in res["plan"]["changes"][0]["id3"]


def test_never_move_out_of_cloud_folder(drive):
    res = server.organize_library_by_genre(assignments={"FX - SIREN": "Tech House"}, move_files=True, dry_run=False)
    t = server.get_track("FX - SIREN")
    assert Path(t["path"]).parent == drive  # still in "Google Drive"
    assert "Google Drive" in res["plan"]["changes"][0]["move"]


def test_dedupe_does_not_trash_cloud_files(drive):
    res = server.dedupe_library(action="remove", dry_run=False)
    assert res["result"]["files_moved_to_trash"] == []
    assert any("Google Drive" in i.get("file_note", "") for i in res["plan"]["items"])
    assert Path(get_config().db_path).exists()


def test_keeper_prefers_local_over_online_only(lib, monkeypatch):
    real = cloud.file_state
    monkeypatch.setattr(cloud, "file_state",
                        lambda p: "online_only" if p and p.endswith("Demo Track 1.mp3") else real(p))
    rep = server.dedupe_library(action="report")
    grp = [g for g in rep["report"] if any("Demo Track 1" in t["track"] for t in g["tracks"])][0]
    keeper = [t for t in grp["tracks"] if t["keeper"]][0]
    assert keeper["file_state"] == "local" and "(copy)" in keeper["path"]
