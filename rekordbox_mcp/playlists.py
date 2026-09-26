"""Playlist and playlist-folder operations."""

from __future__ import annotations

from typing import Any

from .db import active, file_exists, track_label, track_to_dict

ATTR_PLAYLIST = 0
ATTR_FOLDER = 1
ATTR_SMART = 4
_KIND = {ATTR_PLAYLIST: "playlist", ATTR_FOLDER: "folder", ATTR_SMART: "smart_playlist"}


class PlaylistError(ValueError):
    pass


def _visible_playlists(db: Any) -> list[Any]:
    return [p for p in active(db.get_playlist().all()) if (p.Attribute or 0) >= 0]


def playlist_path(p: Any, by_id: dict[str, Any]) -> str:
    parts = [p.Name]
    seen = {str(p.ID)}
    parent = by_id.get(str(p.ParentID))
    while parent is not None and str(parent.ID) not in seen:
        parts.append(parent.Name)
        seen.add(str(parent.ID))
        parent = by_id.get(str(parent.ParentID))
    return "/".join(reversed(parts))


def playlist_songs(db: Any, playlist: Any) -> list[Any]:
    songs = active(db.get_playlist_songs(PlaylistID=playlist.ID).all())
    return sorted(songs, key=lambda s: s.TrackNo or 0)


def list_playlists(db: Any, include_tracks: bool = False) -> list[dict[str, Any]]:
    pls = _visible_playlists(db)
    by_id = {str(p.ID): p for p in pls}
    out = []
    for p in sorted(pls, key=lambda p: (playlist_path(p, by_id).lower())):
        kind = _KIND.get(p.Attribute, f"attr_{p.Attribute}")
        entry: dict[str, Any] = {
            "id": str(p.ID),
            "name": p.Name,
            "path": playlist_path(p, by_id),
            "type": kind,
            "parent_id": str(p.ParentID),
        }
        if kind == "playlist":
            songs = playlist_songs(db, p)
            entry["track_count"] = len(songs)
            if include_tracks:
                entry["tracks"] = [track_label(s.Content) for s in songs if s.Content is not None]
        elif kind == "folder":
            entry["children"] = sum(1 for c in pls if str(c.ParentID) == str(p.ID))
        out.append(entry)
    return out


def resolve_playlist(db: Any, ref: str | int, want: str | None = None) -> Any:
    """Find a playlist by ID, exact path ('Sets/Friday') or unique name."""
    pls = _visible_playlists(db)
    by_id = {str(p.ID): p for p in pls}
    ref_s = str(ref).strip()
    matches = []
    if ref_s in by_id:
        matches = [by_id[ref_s]]
    else:
        low = ref_s.lower().strip("/")
        matches = [p for p in pls if playlist_path(p, by_id).lower() == low]
        if not matches:
            matches = [p for p in pls if (p.Name or "").lower() == low]
    if want == "folder":
        matches = [p for p in matches if p.Attribute == ATTR_FOLDER]
    elif want == "playlist":
        matches = [p for p in matches if p.Attribute == ATTR_PLAYLIST]
    if not matches:
        raise PlaylistError(f"{want or 'playlist'} '{ref_s}' niet gevonden")
    if len(matches) > 1:
        raise PlaylistError(
            f"'{ref_s}' is niet uniek: " + ", ".join(f"{playlist_path(p, by_id)} (id {p.ID})" for p in matches)
            + ". Gebruik het volledige pad of het ID."
        )
    return matches[0]


def playlist_tracks(db: Any, ref: str | int) -> dict[str, Any]:
    pl = resolve_playlist(db, ref)
    songs = playlist_songs(db, pl)
    return {
        "id": str(pl.ID),
        "name": pl.Name,
        "tracks": [
            {"position": s.TrackNo, "song_entry_id": str(s.ID), **track_to_dict(s.Content)}
            for s in songs
            if s.Content is not None
        ],
    }


def find_child(db: Any, parent: Any | None, name: str) -> Any | None:
    parent_id = str(parent.ID) if parent is not None else "root"
    for p in _visible_playlists(db):
        if str(p.ParentID) == parent_id and (p.Name or "").lower() == name.lower():
            return p
    return None


def ensure_folder_path(db: Any, folder_path: str | None, dry_run: bool, created: list[str]) -> Any | None:
    """Resolve 'A/B/C' creating missing folders (only when not dry_run)."""
    if not folder_path:
        return None
    parent = None
    for part in [p for p in folder_path.strip("/").split("/") if p]:
        node = find_child(db, parent, part)
        if node is not None and node.Attribute != ATTR_FOLDER:
            raise PlaylistError(f"'{part}' bestaat al maar is geen folder")
        if node is None:
            created.append(part)
            if dry_run:
                # the rest of the path cannot exist yet either
                return None
            node = db.create_playlist_folder(part, parent=parent)
        parent = node
    return parent


def add_tracks(db: Any, playlist: Any, tracks: list[Any], position: int | None = None,
               allow_duplicates: bool = False) -> list[str]:
    existing_ids = {str(s.ContentID) for s in playlist_songs(db, playlist)}
    added = []
    pos = position
    for t in tracks:
        if not allow_duplicates and str(t.ID) in existing_ids:
            continue
        db.add_to_playlist(playlist, t, track_no=pos)
        existing_ids.add(str(t.ID))
        added.append(track_label(t))
        if pos is not None:
            pos += 1
    return added


def plan_add(db: Any, playlist: Any | None, tracks: list[Any], allow_duplicates: bool = False) -> dict[str, Any]:
    existing_ids = {str(s.ContentID) for s in playlist_songs(db, playlist)} if playlist is not None else set()
    to_add, skipped, missing_files = [], [], []
    for t in tracks:
        if not allow_duplicates and str(t.ID) in existing_ids:
            skipped.append(track_label(t))
            continue
        existing_ids.add(str(t.ID))
        to_add.append(track_label(t))
        if not file_exists(t.FolderPath):
            missing_files.append(track_label(t))
    return {"to_add": to_add, "skipped_already_in_playlist": skipped, "warning_file_missing_on_disk": missing_files}
