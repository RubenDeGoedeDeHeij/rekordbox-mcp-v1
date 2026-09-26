"""Library organisation: genre tags (ID3 + Rekordbox), folder structure, dedupe."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from .db import active, bpm_from_db, file_exists, tables, track_label

# --------------------------------------------------------------------------------------
# Genre tags
# --------------------------------------------------------------------------------------


def read_file_genre(path: str) -> str | None:
    try:
        from mutagen import File

        f = File(path, easy=True)
        if f is not None and f.tags is not None and "genre" in f.tags:
            vals = f.tags["genre"]
            return vals[0] if vals else None
        f = File(path)
        if f is not None and f.tags is not None and hasattr(f.tags, "getall"):
            frames = f.tags.getall("TCON")
            if frames and frames[0].text:
                return str(frames[0].text[0])
    except Exception:
        return None
    return None


def write_file_genre(path: str, genre: str) -> None:
    """Write the genre tag into the audio file (ID3 TCON / Vorbis / MP4)."""
    from mutagen import File
    from mutagen.id3 import TCON

    f = File(path, easy=True)
    if f is not None:
        if f.tags is None:
            f.add_tags()
        f["genre"] = [genre]
        f.save()
        return
    # AIFF/WAV have no "easy" interface: write the ID3 frame directly
    f = File(path)
    if f is None:
        raise ValueError(f"Bestandstype niet ondersteund door mutagen: {path}")
    if f.tags is None:
        f.add_tags()
    f.tags.setall("TCON", [TCON(encoding=3, text=[genre])])
    f.save()


def get_or_create_genre(db: Any, name: str) -> Any:
    existing = [g for g in active(db.get_genre(Name=name).all())]
    if existing:
        return existing[0]
    return db.add_genre(name)


def _safe_dirname(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "-", name).strip().strip(".")
    return name or "Unknown"


def plan_genre_changes(
    db: Any,
    targets: list[tuple[Any, str | None]],
    source: str,
    move_files: bool,
    library_root: Path,
) -> list[dict[str, Any]]:
    """targets = [(content, explicit_genre_or_None)]"""
    changes = []
    for c, explicit in targets:
        exists = file_exists(c.FolderPath)
        if explicit:
            genre = explicit
        elif source == "id3":
            genre = read_file_genre(c.FolderPath) if exists else None
        elif source == "folder":
            genre = Path(c.FolderPath).parent.name if c.FolderPath else None
        elif source == "rekordbox":
            genre = c.GenreName
        else:
            genre = None
        entry: dict[str, Any] = {
            "track": track_label(c),
            "id": str(c.ID),
            "current_rekordbox_genre": c.GenreName,
            "current_file_genre": read_file_genre(c.FolderPath) if exists else None,
            "new_genre": genre,
            "file_exists": exists,
        }
        if genre is None:
            entry["skip"] = "geen genre bepaald"
        elif move_files:
            if not exists:
                entry["move"] = "overgeslagen: bestand bestaat niet lokaal"
            else:
                dst = library_root / _safe_dirname(genre) / Path(c.FolderPath).name
                if Path(c.FolderPath).resolve() != dst.resolve():
                    entry["move_to"] = str(dst)
                    if dst.exists():
                        entry["move"] = "overgeslagen: doelbestand bestaat al"
                        entry.pop("move_to")
        changes.append(entry)
    return changes


def apply_genre_changes(
    db: Any,
    contents: dict[str, Any],
    changes: list[dict[str, Any]],
    write_id3: bool,
    write_rekordbox: bool,
    moved: list[tuple[str, str]],
) -> dict[str, Any]:
    done: dict[str, list] = {"rekordbox_genre": [], "id3_genre": [], "moved": [], "errors": []}
    for ch in changes:
        genre = ch.get("new_genre")
        if not genre:
            continue
        c = contents[ch["id"]]
        if write_rekordbox and c.GenreName != genre:
            g = get_or_create_genre(db, genre)
            c.GenreID = g.ID
            done["rekordbox_genre"].append(ch["track"])
        if write_id3 and ch["file_exists"] and ch.get("current_file_genre") != genre:
            try:
                write_file_genre(c.FolderPath, genre)
                done["id3_genre"].append(ch["track"])
            except Exception as exc:
                done["errors"].append(f"{ch['track']}: ID3 schrijven mislukt: {exc}")
        dst = ch.get("move_to")
        if dst:
            src = c.FolderPath
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
            moved.append((src, dst))
            update_track_path(db, c, dst)
            done["moved"].append(f"{src} -> {dst}")
    return done


def update_track_path(db: Any, c: Any, dst: str) -> None:
    """Point a track at its new file location. With ANLZ files present pyrekordbox
    also rewrites the path tag inside them; commit happens once at the end."""
    if c.AnalysisDataPath and db.get_anlz_dir(c).exists():
        db.update_content_path(c, dst, save=True, check_path=True, commit=False)
        return
    old = c.FolderPath
    c.FolderPath = str(dst)
    if c.OrgFolderPath == old:
        c.OrgFolderPath = str(dst)
    c.FileNameL = Path(dst).name


def undo_moves(moved: list[tuple[str, str]]) -> None:
    for src, dst in reversed(moved):
        if os.path.exists(dst) and not os.path.exists(src):
            shutil.move(dst, src)


# --------------------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------------------

LOSSLESS = {5, 11, 12}  # FLAC, WAV, AIFF

# Rows that should follow the keeper instead of being dropped (table -> container column)
REASSIGN = {"djmdSongPlaylist": "PlaylistID", "djmdSongHistory": "HistoryID", "djmdSongMyTag": "MyTagID"}


def _content_tables() -> list[Any]:
    import inspect

    out = []
    for _n, cls in inspect.getmembers(tables, inspect.isclass):
        if hasattr(cls, "__tablename__") and hasattr(cls, "ContentID") and cls.__tablename__ != "djmdContent":
            out.append(cls)
    return out


def _dedupe_title(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"\(original mix\)", "", text)
    text = re.sub(r"[^a-z0-9()]+", " ", text)
    return " ".join(text.split())


def _norm_path(p: str | None) -> str:
    return (p or "").replace("\\", "/").lower()


def find_duplicate_groups(tracks: list[Any], match: str, duration_tolerance: float) -> list[list[Any]]:
    groups: list[list[Any]] = []
    seen: set[str] = set()

    if match in ("same_file", "both"):
        by_path: dict[str, list[Any]] = {}
        for t in tracks:
            if t.FolderPath:
                by_path.setdefault(_norm_path(t.FolderPath), []).append(t)
        for g in by_path.values():
            if len(g) > 1:
                groups.append(g)
                seen.update(str(t.ID) for t in g)

    if match in ("artist_title", "both"):
        by_key: dict[tuple[str, str], list[Any]] = {}
        for t in tracks:
            if str(t.ID) in seen:
                continue
            key = (_dedupe_title(t.ArtistName), _dedupe_title(t.Title))
            if not key[1]:
                continue
            by_key.setdefault(key, []).append(t)
        for g in by_key.values():
            if len(g) < 2:
                continue
            # split on duration so radio edit vs extended mix are NOT merged
            g = sorted(g, key=lambda t: t.Length or 0)
            cluster = [g[0]]
            for t in g[1:]:
                if abs((t.Length or 0) - (cluster[0].Length or 0)) <= duration_tolerance:
                    cluster.append(t)
                else:
                    if len(cluster) > 1:
                        groups.append(cluster)
                    cluster = [t]
            if len(cluster) > 1:
                groups.append(cluster)
    return groups


def _ref_counts(db: Any, content_id: str) -> dict[str, int]:
    return {
        "playlists": db.query(tables.DjmdSongPlaylist).filter_by(ContentID=content_id).count(),
        "cues": db.query(tables.DjmdCue).filter_by(ContentID=content_id).count(),
    }


def choose_keeper(db: Any, group: list[Any], library_root: Path) -> tuple[Any, list[dict[str, Any]], str]:
    """Pick the keeper. Existence on disk is checked FIRST: a dead database
    reference can never win against a copy whose file really exists."""
    infos = []
    for t in group:
        exists = file_exists(t.FolderPath)
        refs = _ref_counts(db, str(t.ID))
        in_root = bool(t.FolderPath) and _norm_path(t.FolderPath).startswith(_norm_path(str(library_root)))
        infos.append({"t": t, "exists": exists, "refs": refs, "in_library_root": in_root})

    existing = [i for i in infos if i["exists"]]
    pool = existing or infos
    reason = "bestand bestaat" if existing else "GEEN van de bestanden bestaat lokaal — keeper op basis van referenties"

    def score(i: dict[str, Any]) -> tuple:
        t = i["t"]
        return (
            i["in_library_root"],
            (t.FileType in LOSSLESS),
            t.BitRate or 0,
            i["refs"]["cues"] > 0,
            i["refs"]["playlists"] + i["refs"]["cues"],
            t.FileSize or 0,
            -(t.created_at.timestamp() if getattr(t, "created_at", None) else 0),
        )

    keeper = max(pool, key=score)["t"]
    report = [
        {
            "track": track_label(i["t"]),
            "path": i["t"].FolderPath,
            "exists": i["exists"],
            "bitrate": i["t"].BitRate,
            "bpm": bpm_from_db(i["t"].BPM),
            "length_sec": i["t"].Length,
            "playlists": i["refs"]["playlists"],
            "cues": i["refs"]["cues"],
            "keeper": i["t"] is keeper,
        }
        for i in infos
    ]
    return keeper, report, reason


def merge_into_keeper(db: Any, keeper: Any, loser: Any) -> dict[str, int]:
    """Move playlist/history/mytag references to the keeper, drop the rest,
    then ``db.delete(loser)``. No commit here — caller commits once."""
    stats = {"reassigned": 0, "deleted_rows": 0}
    kid, lid = str(keeper.ID), str(loser.ID)
    for cls in _content_tables():
        rows = db.query(cls).filter_by(ContentID=lid).all()
        if not rows:
            continue
        container = REASSIGN.get(cls.__tablename__)
        for row in rows:
            if container:
                cont_id = getattr(row, container)
                dup = db.query(cls).filter_by(ContentID=kid, **{container: cont_id}).first()
                if dup is None:
                    row.ContentID = kid
                    stats["reassigned"] += 1
                    continue
                if cls.__tablename__ == "djmdSongPlaylist":
                    db.remove_from_playlist(cont_id, row)  # renumbers the rest of the playlist
                    stats["deleted_rows"] += 1
                    continue
            db.delete(row)
            stats["deleted_rows"] += 1
    db.delete(loser)
    return stats
