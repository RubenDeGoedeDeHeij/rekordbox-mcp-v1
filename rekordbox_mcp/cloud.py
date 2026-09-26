"""Rekordbox Cloud Library Sync + cloud-storage (Google Drive / Dropbox) awareness.

Two separate things matter here:

* **Library sync.** Rekordbox syncs master.db between devices using per-row
  sequence numbers (``usn`` / ``rb_local_usn``) and a soft-delete flag
  (``rb_local_deleted``). A hard ``DELETE`` leaves nothing for the sync to
  pick up, so a removed row can come back from the cloud. When sync is active
  we therefore soft-delete, like Rekordbox itself does.
* **Online-only files.** Google Drive for desktop and Dropbox (both via macOS
  File Provider, ``~/Library/CloudStorage/...``; legacy ``~/Dropbox`` is also
  recognised as a cloud folder) keep "streamed" files as placeholders: ``Path.exists()`` is True
  but the audio is not on disk. Writing tags would force a download and moving
  the file out of the cloud folder breaks the link, so those are skipped.

Legacy Dropbox Smart Sync (pre-File-Provider) sets no "dataless" flag; there
online-only detection falls back to ``st_blocks == 0``.

The sync protocol is not documented; everything here is based on what the
database itself shows (see :func:`cloud_sync_status`).
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Any

from .db import tables

SF_DATALESS = 0x40000000  # macOS: file content is not materialised locally

# table name -> mapped class, for the tables we may delete from
_DELETE_TABLES = {
    "djmdContent": tables.DjmdContent,
    "djmdPlaylist": tables.DjmdPlaylist,
    "djmdSongPlaylist": tables.DjmdSongPlaylist,
    "djmdCue": tables.DjmdCue,
}


# --------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------


def cloud_storage_roots() -> list[Path]:
    home = Path.home()
    patterns = [
        str(home / "Library" / "CloudStorage" / "GoogleDrive-*"),
        str(home / "Library" / "CloudStorage" / "Dropbox*"),
        str(home / "Google Drive"),
        str(home / "Dropbox"),
    ]
    roots = [Path(p) for pat in patterns for p in glob.glob(pat)]
    extra = os.environ.get("RBMCP_CLOUD_ROOTS", "")
    roots += [Path(p).expanduser() for p in extra.split(os.pathsep) if p.strip()]
    return roots


def in_cloud_storage(path: str | None, roots: list[Path] | None = None) -> bool:
    if not path:
        return False
    p = str(Path(path)).replace("\\", "/").lower()
    for root in roots if roots is not None else cloud_storage_roots():
        r = str(root).replace("\\", "/").lower().rstrip("/")
        if p == r or p.startswith(r + "/"):
            return True
    # fallback for paths written on another device (the folder need not exist here)
    markers = ("/library/cloudstorage/", "/google drive/", "/my drive/", "/dropbox/")
    return any(m in p for m in markers) or p.endswith("/dropbox")


def file_state(path: str | None) -> str:
    """'local', 'online_only' (cloud placeholder) or 'missing'."""
    if not path:
        return "missing"
    try:
        st = os.stat(path)
    except OSError:
        return "missing"
    flags = getattr(st, "st_flags", 0) or 0
    if flags & SF_DATALESS:
        return "online_only"
    if st.st_size > 0 and getattr(st, "st_blocks", 1) == 0:
        return "online_only"
    return "local"


# --------------------------------------------------------------------------------------
# Library sync
# --------------------------------------------------------------------------------------


def _count(db: Any, cls: Any, *criteria: Any) -> int:
    return db.query(cls).filter(*criteria).count()


def cloud_sync_status(db: Any) -> dict[str, Any]:
    from .safety import find_processes

    synced_content = _count(db, tables.DjmdContent, tables.DjmdContent.rb_local_synced == 1)
    usn_content = _count(db, tables.DjmdContent, tables.DjmdContent.usn.isnot(None))
    # Attribute < 0 are Rekordbox's built-in system nodes (e.g. the "Cloud Library Sync"
    # trial node every install has, which carries a usn): not evidence of real sync use
    user_pl = tables.DjmdPlaylist.Attribute >= 0
    synced_pl = _count(db, tables.DjmdPlaylist, user_pl, tables.DjmdPlaylist.rb_local_synced == 1)
    usn_pl = _count(db, tables.DjmdPlaylist, user_pl, tables.DjmdPlaylist.usn.isnot(None))
    soft_deleted = {name: _count(db, cls, cls.rb_local_deleted == 1) for name, cls in _DELETE_TABLES.items()}
    agent = find_processes({"rekordboxagent", "rekordboxagent.exe"})
    evidence = {
        "content_rows_synced": synced_content,
        "content_rows_with_cloud_usn": usn_content,
        "playlist_rows_synced": synced_pl,
        "playlist_rows_with_cloud_usn": usn_pl,
        "soft_deleted_rows_by_rekordbox": soft_deleted,
        "rekordbox_agent_running": bool(agent),
    }
    active = (synced_content + usn_content + synced_pl + usn_pl) > 0
    return {"sync_active": active, "evidence": evidence}


def delete_mode(db: Any, table: str, status: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return ('soft'|'hard', reason) for deletes in ``table``."""
    forced = os.environ.get("RBMCP_DELETE_MODE", "auto").lower()
    if forced in ("soft", "hard"):
        return forced, f"geforceerd via RBMCP_DELETE_MODE={forced}"
    status = status or cloud_sync_status(db)
    if not status["sync_active"]:
        return "hard", "geen Cloud Library Sync gedetecteerd"
    if status["evidence"]["soft_deleted_rows_by_rekordbox"].get(table, 0) > 0:
        return "soft", "Cloud Library Sync actief en Rekordbox gebruikt zelf soft delete in deze tabel"
    # sync is on but no proof for this table: still the safer choice for sync
    return "soft", "Cloud Library Sync actief (geen eigen soft-deletes in deze tabel gezien — onbevestigd)"


def soft_delete(row: Any) -> None:
    row.rb_local_deleted = 1


def remove(db: Any, row: Any, mode: str) -> None:
    if mode == "soft":
        soft_delete(row)
    else:
        db.delete(row)


def remove_song_from_playlist(db: Any, playlist_id: str, song: Any, mode: str) -> None:
    if mode == "hard":
        db.remove_from_playlist(playlist_id, song)
        return
    track_no = song.TrackNo or 0
    soft_delete(song)
    for other in db.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=str(playlist_id)).all():
        if other is not song and not other.rb_local_deleted and (other.TrackNo or 0) > track_no:
            other.TrackNo -= 1


def delete_playlist(db: Any, playlist: Any, mode: str) -> list[str]:
    """Delete a playlist/folder (+ children and their song entries)."""
    if mode == "hard":
        db.delete_playlist(playlist)
        return [str(playlist.ID)]
    # renumber the siblings like Rekordbox does
    for sib in db.query(tables.DjmdPlaylist).filter(
        tables.DjmdPlaylist.ParentID == playlist.ParentID, tables.DjmdPlaylist.Seq > playlist.Seq
    ):
        if not sib.rb_local_deleted:
            sib.Seq -= 1
    ids, todo = [], [playlist]
    while todo:
        node = todo.pop()
        ids.append(str(node.ID))
        soft_delete(node)
        for song in db.query(tables.DjmdSongPlaylist).filter_by(PlaylistID=str(node.ID)).all():
            soft_delete(song)
        todo.extend(c for c in node.Children if not c.rb_local_deleted)
    if getattr(db, "playlist_xml", None) is not None:
        for pid in ids:
            try:
                db.playlist_xml.remove(pid)
            except Exception:
                pass
    return ids


def summarize_files(tracks: list[Any]) -> dict[str, int]:
    roots = cloud_storage_roots()
    out = {"local": 0, "online_only": 0, "missing": 0, "in_cloud_storage": 0}
    for t in tracks:
        out[file_state(t.FolderPath)] += 1
        if in_cloud_storage(t.FolderPath, roots):
            out["in_cloud_storage"] += 1
    return out
