"""Rekordbox library management MCP server (stdio).

Every tool that writes goes through :func:`rekordbox_mcp.safety.guarded_write`:
Rekordbox must be closed, a verified backup (master.db + ANLZ + settings) is
made first, and the action is logged. All write tools default to dry_run=True.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

from . import anlz, library, playlists, safety, sets
from . import settings as rb_settings
from .config import get_config
from .db import (
    all_tracks,
    file_exists,
    open_db,
    resolve_track,
    resolve_tracks,
    search_tracks as _search,
    track_label,
    track_to_dict,
)

INSTRUCTIONS = """Beheer van een Rekordbox 6/7-bibliotheek via pyrekordbox.
Schrijftools hebben dry_run=true als standaard: toon eerst de dry-run aan de gebruiker en
voer pas uit (dry_run=false / mode='execute') na bevestiging. Rekordbox moet gesloten zijn
voor schrijfacties; er wordt automatisch een backup gemaakt. BPM's zijn gewone BPM (128.0)."""

mcp = _Server("rekordbox", instructions=INSTRUCTIONS)


def _out(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _err(exc: Exception) -> str:
    payload: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
    if getattr(exc, "candidates", None):
        payload["candidates"] = exc.candidates  # type: ignore[attr-defined]
    return _out(payload)


def _tool(fn):
    """Register a tool and turn exceptions into readable JSON errors."""
    import inspect

    def wrapper(*args, **kwargs):
        try:
            return _out(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - reported back to the client
            return _err(exc)

    # expose the original parameters, but declare a plain-text (JSON string) result
    wrapper.__name__ = fn.__name__
    wrapper.__qualname__ = fn.__qualname__
    wrapper.__doc__ = fn.__doc__
    wrapper.__module__ = fn.__module__
    wrapper.__annotations__ = {**fn.__annotations__, "return": "str"}
    wrapper.__signature__ = inspect.signature(fn).replace(return_annotation=str)  # type: ignore[attr-defined]
    mcp.tool()(wrapper)
    return fn


# ======================================================================================
# Status / read-only
# ======================================================================================


@_tool
def get_status() -> dict:
    """Paden, Rekordbox-processtatus, aantal tracks/playlists en laatste backup."""
    cfg = get_config()
    info: dict[str, Any] = {
        "db_path": str(cfg.db_path),
        "db_exists": bool(cfg.db_path and cfg.db_path.exists()),
        "anlz_dir": str(cfg.anlz_dir) if cfg.db_path else None,
        "backup_dir": str(cfg.backup_dir),
        "log_file": str(cfg.log_file),
        "library_root": str(cfg.library_root),
        "rekordbox_running": safety.find_rekordbox_processes(),
        "platform": platform.platform(),
    }
    with open_db(cfg) as db:
        tracks = all_tracks(db)
        info["tracks"] = len(tracks)
        info["tracks_missing_file"] = sum(1 for t in tracks if not file_exists(t.FolderPath))
        info["playlists"] = len(playlists.list_playlists(db))
    backups = safety.list_backups(cfg)
    info["backups"] = len(backups)
    info["last_backup"] = backups[0]["backup_path"] if backups else None
    return info


@_tool
def list_playlists(include_tracks: bool = False) -> list:
    """Alle playlists en folders met pad, type en aantal tracks."""
    with open_db() as db:
        return playlists.list_playlists(db, include_tracks=include_tracks)


@_tool
def get_playlist_tracks(playlist: str) -> dict:
    """Tracks van een playlist in afspeelvolgorde. playlist = ID, pad ('Sets/Vrijdag') of naam."""
    with open_db() as db:
        return playlists.playlist_tracks(db, playlist)


@_tool
def search_tracks(
    query: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    genres: list[str] | None = None,
    key: str | None = None,
    energy_min: int | None = None,
    energy_max: int | None = None,
    rating_min: int | None = None,
    only_existing_files: bool = False,
    limit: int = 50,
) -> dict:
    """Zoek tracks op tekst (artiest/titel/album/pad), BPM-range, genre(s), key (Am / 8A),
    energy (uit comment, Mixed In Key-stijl) of rating."""
    with open_db() as db:
        res = _search(db, query, bpm_min, bpm_max, genres, key, energy_min, energy_max, rating_min, only_existing_files)
        return {"count": len(res), "tracks": [track_to_dict(t) for t in res[:limit]]}


@_tool
def get_track(track: str) -> dict:
    """Alle details van één track (ID of 'Artiest - Titel')."""
    with open_db() as db:
        return track_to_dict(resolve_track(db, track), verbose=True)


# ======================================================================================
# Playlists (write)
# ======================================================================================


@_tool
def create_playlist(name: str, parent: str | None = None, is_folder: bool = False, dry_run: bool = True) -> dict:
    """Maak een playlist of playlist-folder. parent = folderpad ('Sets/2026'); ontbrekende
    folders worden aangemaakt."""
    with open_db() as db:
        created_folders: list[str] = []
        parent_node = playlists.ensure_folder_path(db, parent, True, created_folders)
        parent_exists = parent is None or parent_node is not None
        if parent_exists and playlists.find_child(db, parent_node, name) is not None:
            raise playlists.PlaylistError(f"'{name}' bestaat al in {parent or 'root'}")
        plan = {"create": "folder" if is_folder else "playlist", "name": name, "parent": parent or "root",
                "also_create_folders": created_folders}

        def apply() -> dict:
            folders: list[str] = []
            node = playlists.ensure_folder_path(db, parent, False, folders)
            pl = db.create_playlist_folder(name, parent=node) if is_folder else db.create_playlist(name, parent=node)
            db.commit()
            return {"id": str(pl.ID), "created_folders": folders}

        return safety.guarded_write("create_playlist", dry_run=dry_run, plan=plan, apply=apply,
                                    rollback=db.rollback)


@_tool
def delete_playlist(playlist: str, dry_run: bool = True) -> dict:
    """Verwijder een playlist of folder (folder: inclusief inhoud). Tracks blijven in de collectie."""
    with open_db() as db:
        pl = playlists.resolve_playlist(db, playlist)
        plan = {"delete": playlist, "id": str(pl.ID), "type": "folder" if pl.Attribute == 1 else "playlist"}
        if pl.Attribute == 0:
            plan["tracks_in_playlist"] = len(playlists.playlist_songs(db, pl))

        def apply() -> dict:
            db.delete_playlist(pl)
            db.commit()
            return {"deleted": str(pl.ID)}

        return safety.guarded_write("delete_playlist", dry_run=dry_run, plan=plan, apply=apply, rollback=db.rollback)


@_tool
def add_tracks_to_playlist(
    playlist: str,
    tracks: list[str],
    position: int | None = None,
    allow_duplicates: bool = False,
    create_if_missing: bool = False,
    dry_run: bool = True,
) -> dict:
    """Voeg tracks toe (IDs of 'Artiest - Titel', in deze volgorde). position = 1-based
    invoegpositie (standaard achteraan). create_if_missing maakt de playlist (pad mag)."""
    with open_db() as db:
        found, errors = resolve_tracks(db, tracks)
        if errors:
            raise ValueError(_out({"unresolved_tracks": errors}))
        try:
            pl = playlists.resolve_playlist(db, playlist, want="playlist")
        except playlists.PlaylistError:
            if not create_if_missing:
                raise
            pl = None
        plan = {"playlist": playlist, "create_playlist": pl is None, "position": position or "end",
                **playlists.plan_add(db, pl, found, allow_duplicates)}

        def apply() -> dict:
            target = pl
            if target is None:
                parts = playlist.strip("/").split("/")
                node = playlists.ensure_folder_path(db, "/".join(parts[:-1]), False, [])
                target = db.create_playlist(parts[-1], parent=node)
            added = playlists.add_tracks(db, target, found, position, allow_duplicates)
            db.commit()
            return {"playlist_id": str(target.ID), "added": added}

        return safety.guarded_write("add_tracks_to_playlist", dry_run=dry_run, plan=plan, apply=apply,
                                    tracks=[track_label(t) for t in found], rollback=db.rollback)


@_tool
def remove_tracks_from_playlist(playlist: str, tracks: list[str], dry_run: bool = True) -> dict:
    """Haal tracks uit een playlist (alleen de playlist-entry, niet uit de collectie)."""
    with open_db() as db:
        pl = playlists.resolve_playlist(db, playlist, want="playlist")
        found, errors = resolve_tracks(db, tracks)
        if errors:
            raise ValueError(_out({"unresolved_tracks": errors}))
        ids = {str(t.ID) for t in found}
        songs = [s for s in playlists.playlist_songs(db, pl) if str(s.ContentID) in ids]
        plan = {"playlist": pl.Name, "remove": [f"#{s.TrackNo} {track_label(s.Content)}" for s in songs],
                "not_in_playlist": [track_label(t) for t in found if str(t.ID) not in {str(s.ContentID) for s in songs}]}

        def apply() -> dict:
            for s in songs:
                db.remove_from_playlist(pl, s)
            db.commit()
            return {"removed": len(songs)}

        return safety.guarded_write("remove_tracks_from_playlist", dry_run=dry_run, plan=plan, apply=apply,
                                    tracks=[track_label(t) for t in found], rollback=db.rollback)


@_tool
def reorder_playlist(playlist: str, order: list[str] | None = None, sort_by: str | None = None,
                     dry_run: bool = True) -> dict:
    """Herorden een playlist. Óf `order` = volledige/gedeeltelijke lijst tracks in gewenste volgorde
    (niet-genoemde tracks schuiven achteraan), óf `sort_by` = bpm_ascending | bpm_descending |
    energy_ramp | warmup_peak_cooldown | harmonic."""
    if not order and not sort_by:
        raise ValueError("Geef 'order' of 'sort_by'")
    with open_db() as db:
        pl = playlists.resolve_playlist(db, playlist, want="playlist")
        songs = playlists.playlist_songs(db, pl)
        contents = [s.Content for s in songs]
        if order:
            wanted, errors = resolve_tracks(db, order)
            if errors:
                raise ValueError(_out({"unresolved_tracks": errors}))
            wanted_ids = [str(t.ID) for t in wanted]
            new = [c for wid in wanted_ids for c in contents if str(c.ID) == wid]
            new += [c for c in contents if str(c.ID) not in wanted_ids]
        else:
            new = sets.order_tracks(contents, sort_by)
        plan = {"playlist": pl.Name, "old_order": [track_label(c) for c in contents],
                "new_order": [track_label(c) for c in new]}

        def apply() -> dict:
            by_content = {str(s.ContentID): s for s in songs}
            for pos, c in enumerate(new, 1):
                db.move_song_in_playlist(pl, by_content[str(c.ID)], pos)
            db.commit()
            return {"reordered": len(new)}

        return safety.guarded_write("reorder_playlist", dry_run=dry_run, plan=plan, apply=apply, rollback=db.rollback)


# ======================================================================================
# DJ sets
# ======================================================================================


@_tool
def build_set_from_criteria(
    name: str,
    track_names: list[str] | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    genres: list[str] | None = None,
    key: str | None = None,
    energy_min: int | None = None,
    energy_max: int | None = None,
    rating_min: int | None = None,
    ordering: str = "warmup_peak_cooldown",
    max_tracks: int | None = None,
    target_minutes: float | None = None,
    parent_folder: str | None = "Sets",
    include_missing_files: bool = False,
    dry_run: bool = True,
) -> dict:
    """Bouw een DJ-set-playlist. Óf `track_names` (volgorde = as_given tenzij anders gekozen), óf
    criteria (BPM/genre/key/energy/rating). ordering: as_given | bpm_ascending | bpm_descending |
    energy_ramp | warmup_peak_cooldown | harmonic. Energy komt uit het comment-veld ('Energy 7',
    Mixed In Key); ontbreekt dat, dan valt energy_ramp terug op rating. Tracks waarvan het bestand
    lokaal niet bestaat worden overgeslagen (tenzij include_missing_files)."""
    if ordering not in sets.ORDERINGS:
        raise ValueError(f"ordering moet een van {sets.ORDERINGS} zijn")
    with open_db() as db:
        unresolved: list[dict] = []
        if track_names:
            chosen, unresolved = resolve_tracks(db, track_names)
            if ordering == "warmup_peak_cooldown":
                ordering = "as_given"
        else:
            if not any(v is not None for v in (bpm_min, bpm_max, genres, key, energy_min, energy_max, rating_min)):
                raise ValueError("Geef track_names of minstens één criterium")
            chosen = _search(db, None, bpm_min, bpm_max, genres, key, energy_min, energy_max, rating_min)
        skipped_missing = []
        if not include_missing_files:
            skipped_missing = [track_label(t) for t in chosen if not file_exists(t.FolderPath)]
            chosen = [t for t in chosen if file_exists(t.FolderPath)]
        ordered = sets.limit_tracks(sets.order_tracks(chosen, ordering), max_tracks, target_minutes)
        total = sum(float(t.Length or 0) for t in ordered)
        full_path = f"{parent_folder.strip('/')}/{name}" if parent_folder else name
        exists_already = True
        try:
            playlists.resolve_playlist(db, full_path)
        except playlists.PlaylistError:
            exists_already = False
        if exists_already:
            raise playlists.PlaylistError(f"Playlist '{full_path}' bestaat al; kies een andere naam")
        plan = {
            "playlist": full_path,
            "ordering": ordering,
            "track_count": len(ordered),
            "total_minutes": round(total / 60, 1),
            "tracklist": sets.describe_transitions(ordered),
            "unresolved": unresolved,
            "skipped_missing_file": skipped_missing,
        }
        if not ordered:
            raise ValueError(_out({"message": "Geen tracks gevonden voor deze set", **plan}))

        def apply() -> dict:
            node = playlists.ensure_folder_path(db, parent_folder, False, [])
            pl = db.create_playlist(name, parent=node)
            for t in ordered:
                db.add_to_playlist(pl, t)
            db.commit()
            return {"playlist_id": str(pl.ID), "tracks_added": len(ordered)}

        return safety.guarded_write("build_set_from_criteria", dry_run=dry_run, plan=plan, apply=apply,
                                    tracks=[track_label(t) for t in ordered], rollback=db.rollback)


# ======================================================================================
# Library organisation
# ======================================================================================


def _genre_write(action: str, targets_spec: list[tuple[str | None, str | None]], source: str, write_id3: bool,
                 write_rekordbox: bool, move_files: bool, library_root: str | None, dry_run: bool) -> dict:
    cfg = get_config()
    root = Path(library_root).expanduser() if library_root else cfg.library_root
    with open_db(cfg) as db:
        tracks = all_tracks(db)
        targets: list[tuple[Any, str | None]] = []
        errors = []
        if targets_spec and targets_spec[0][0] is not None:
            for q, g in targets_spec:
                try:
                    targets.append((resolve_track(db, q, tracks), g))
                except ValueError as exc:
                    errors.append(str(exc))
        else:
            genre_filter = targets_spec[0][1] if targets_spec else None
            targets = [(t, None) for t in tracks if genre_filter is None or (t.GenreName or "") == genre_filter]
        changes = library.plan_genre_changes(db, targets, source, move_files, root)
        effective = [c for c in changes if c.get("new_genre") and (
            c["new_genre"] != c["current_rekordbox_genre"] or c["new_genre"] != c["current_file_genre"] or c.get("move_to"))]
        plan = {"library_root": str(root), "write_id3": write_id3, "write_rekordbox": write_rekordbox,
                "move_files": move_files, "changes": effective, "unchanged_or_skipped": len(changes) - len(effective),
                "unresolved": errors}
        contents = {str(c.ID): c for c, _ in targets}
        moved: list[tuple[str, str]] = []

        def apply() -> dict:
            res = library.apply_genre_changes(db, contents, effective, write_id3, write_rekordbox, moved)
            db.commit()
            return res

        def rollback() -> None:
            db.rollback()
            library.undo_moves(moved)

        return safety.guarded_write(action, dry_run=dry_run, plan=plan, apply=apply,
                                    tracks=[c["track"] for c in effective], rollback=rollback)


@_tool
def set_track_genre(tracks: list[str], genre: str, write_id3: bool = True, write_rekordbox: bool = True,
                    dry_run: bool = True) -> dict:
    """Zet het genre van één of meer tracks, in Rekordbox én in de ID3/bestandstags (mutagen)."""
    return _genre_write("set_track_genre", [(t, genre) for t in tracks], "explicit", write_id3, write_rekordbox,
                        False, None, dry_run)


@_tool
def organize_library_by_genre(
    assignments: dict[str, str] | None = None,
    source: str = "rekordbox",
    only_genre: str | None = None,
    write_id3: bool = True,
    write_rekordbox: bool = True,
    move_files: bool = False,
    library_root: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Organiseer de bibliotheek op genre.
    - assignments: {"track (ID of Artiest - Titel)": "Genre"} expliciet zetten; of
    - source: 'rekordbox' (Rekordbox-genre → ID3), 'id3' (ID3-genre → Rekordbox) of 'folder'
      (mapnaam → genre) voor alle tracks (optioneel alleen tracks met only_genre).
    - move_files=true verplaatst bestanden naar <library_root>/<Genre>/ (standaard
      ~/Music/DJ/02 Library) en werkt FolderPath + ANLZ-paden bij."""
    if source not in ("rekordbox", "id3", "folder"):
        raise ValueError("source moet 'rekordbox', 'id3' of 'folder' zijn")
    if assignments:
        spec = [(t, g) for t, g in assignments.items()]
    else:
        spec = [(None, only_genre)]
    if source == "rekordbox" and not assignments:
        write_rekordbox = False
    if source == "id3" and not assignments:
        write_id3 = False
    return _genre_write("organize_library_by_genre", spec, source, write_id3, write_rekordbox, move_files,
                        library_root, dry_run)


@_tool
def dedupe_library(
    match: str = "both",
    duration_tolerance_sec: float = 3.0,
    action: str = "report",
    trash_duplicate_files: bool = True,
    only_groups: list[int] | None = None,
    dry_run: bool = True,
) -> dict:
    """Vind en verwijder dubbele tracks.
    match: 'same_file' (zelfde pad), 'artist_title' (artiest+titel, lengte binnen tolerantie) of 'both'.
    action: 'report' (niets wijzigen) of 'remove' (verliezers samenvoegen in de keeper).
    De keeper wordt pas gekozen NA de check of het bestand bestaat: een dode verwijzing wint nooit
    van een bestaand bestand. Playlist-, history- en MyTag-verwijzingen gaan naar de keeper.
    trash_duplicate_files verplaatst het audiobestand van een verliezer naar duplicates_trash/
    (nooit wissen; nooit als het hetzelfde bestand als de keeper is). only_groups = groepnummers uit
    het rapport om alleen die te verwerken."""
    if match not in ("same_file", "artist_title", "both"):
        raise ValueError("match moet same_file, artist_title of both zijn")
    if action not in ("report", "remove"):
        raise ValueError("action moet 'report' of 'remove' zijn")
    cfg = get_config()
    with open_db(cfg) as db:
        groups = library.find_duplicate_groups(all_tracks(db), match, duration_tolerance_sec)
        report = []
        work = []
        for i, g in enumerate(groups, 1):
            keeper, rows, reason = library.choose_keeper(db, g, cfg.library_root)
            report.append({"group": i, "keeper_reason": reason, "tracks": rows})
            if only_groups is None or i in only_groups:
                work.append((keeper, [t for t in g if t is not keeper]))
        if action == "report":
            return {"groups": len(groups), "report": report,
                    "hint": "Gebruik action='remove' (eerst met dry_run=true) om op te ruimen."}

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        trash_root = cfg.trash_dir / stamp
        plan_items = []
        for keeper, losers in work:
            for loser in losers:
                move = None
                if trash_duplicate_files and file_exists(loser.FolderPath) and (
                    not file_exists(keeper.FolderPath)
                    or Path(loser.FolderPath).resolve() != Path(keeper.FolderPath).resolve()
                ):
                    move = str(trash_root / f"{loser.ID}_{Path(loser.FolderPath).name}")
                plan_items.append({"keeper": track_label(keeper), "remove_db_entry": track_label(loser),
                                   "loser_path": loser.FolderPath, "loser_file_exists": file_exists(loser.FolderPath),
                                   "move_file_to_trash": move})
        plan = {"groups_processed": len(work), "entries_to_remove": len(plan_items), "items": plan_items}
        moved: list[tuple[str, str]] = []

        def apply() -> dict:
            stats = []
            for keeper, losers in work:
                for loser in losers:
                    stats.append({"removed": track_label(loser), **library.merge_into_keeper(db, keeper, loser)})
            db.commit()  # one commit for all deletes
            for item in plan_items:
                if item["move_file_to_trash"]:
                    trash_root.mkdir(parents=True, exist_ok=True)
                    shutil.move(item["loser_path"], item["move_file_to_trash"])
                    moved.append((item["loser_path"], item["move_file_to_trash"]))
            return {"merged": stats, "files_moved_to_trash": moved}

        def rollback() -> None:
            db.rollback()
            library.undo_moves(moved)

        return safety.guarded_write("dedupe_library", dry_run=dry_run, plan=plan, apply=apply,
                                    tracks=[p["remove_db_entry"] for p in plan_items], rollback=rollback)


# ======================================================================================
# Settings
# ======================================================================================


@_tool
def get_settings(file: str | None = None) -> dict:
    """Lees Rekordbox MySettings (MYSETTING, MYSETTING2, DJMMYSETTING, DEVSETTING): huidige waarden
    én toegestane opties per instelling."""
    data = rb_settings.read_settings(file=file)
    if not data:
        return {"message": "Geen MySetting-bestanden gevonden", "searched_db_dir": str(get_config().db_dir)}
    return data


@_tool
def update_setting(key: str, value: str, file: str | None = None, dry_run: bool = True) -> dict:
    """Wijzig één MySetting (bijv. key='quantize', value='on'). Waarde wordt gevalideerd tegen de
    toegestane opties; na schrijven wordt het bestand opnieuw ingelezen ter verificatie."""
    plan = rb_settings.plan_setting_change(key, value, file)
    return safety.guarded_write("update_setting", dry_run=dry_run, plan=plan,
                                apply=lambda: rb_settings.apply_setting_change(plan), include_anlz=False)


# ======================================================================================
# Cues & beatgrids (experimental)
# ======================================================================================


@_tool
def inspect_track_cues(track: str) -> dict:
    """Toon de cues van een track uit de database (djmdCue), contentCue-JSON en ANLZ-bestanden,
    plus de beatgrid-samenvatting. Gebruik dit op een track met handmatig gezette cues om het
    formaat van jouw Rekordbox-versie te verifiëren."""
    with open_db() as db:
        return anlz.inspect_cues(db, resolve_track(db, track))


@_tool
def verify_cue_format() -> dict:
    """Vergelijk djmdCue.Kind met de hot-cue-nummers in de ANLZ-bestanden van je eigen bibliotheek
    om de aangenomen mapping (A,B,C,D.. → Kind 1,2,3,5..) te bevestigen."""
    with open_db() as db:
        return {"assumed_mapping": anlz.HOT_CUE_KIND, **anlz.infer_hot_cue_mapping(db)}


@_tool
def write_cue_points(track: str, cues: list[dict], replace_existing: bool = False, mode: str = "dry_run",
                     force: bool = False) -> dict:
    """EXPERIMENTEEL. Schrijf cue-punten naar de Rekordbox-database (djmdCue).
    cues: [{"type": "hot", "slot": "A", "time": "0:32.5", "color": "green", "comment": "drop"},
           {"type": "memory", "time": 64.0}, {"type": "hot", "slot": "B", "time": 96, "loop_end": 104}]
    mode: 'dry_run' (standaard, schrijft niets) of 'execute'.
    Execute weigert als de hot-cue-mapping niet door je eigen bibliotheek bevestigd wordt of als de
    track contentCue-JSON heeft — tenzij force=true."""
    if mode not in ("dry_run", "execute"):
        raise ValueError("mode moet 'dry_run' of 'execute' zijn")
    with open_db() as db:
        content = resolve_track(db, track)
        plan = anlz.plan_cues(db, content, cues, replace_existing)
        if mode == "execute":
            problems = list(plan["warnings"])
            if plan["conflicts"]:
                problems.append("Er zijn bestaande cues op dezelfde slots/tijden (gebruik replace_existing=true).")
            if any(c["Kind"] > 0 for c in plan["new_cues"]):
                check = anlz.infer_hot_cue_mapping(db)
                plan["hot_cue_mapping_check"] = check
                if not check["confirmed"]:
                    problems.append("Hot-cue-mapping niet bevestigd door je eigen bibliotheek "
                                    f"({check['tracks_compared']} tracks vergeleken, tegenstrijdig: {check['contradictions']}).")
            if problems and not force:
                raise anlz.CueFormatError("Execute geweigerd, er is niets geschreven: " + " | ".join(problems)
                                          + " Gebruik force=true alleen als je dit bewust accepteert.")
        return safety.guarded_write("write_cue_points", dry_run=(mode != "execute"), plan=plan,
                                    apply=lambda: anlz.apply_cues(db, content, plan),
                                    tracks=[track_label(content)], rollback=db.rollback)


@_tool
def write_beatgrid(track: str, bpm: float, first_beat: str, first_beat_number: int = 1,
                   mode: str = "dry_run") -> dict:
    """EXPERIMENTEEL. Herschrijf de beatgrid (constante BPM) in de ANLZ .DAT/.EXT-bestanden en het
    BPM-veld in de database. first_beat = tijd van de eerste beat (seconden of mm:ss.xxx),
    first_beat_number = positie in de maat (1 = downbeat). Aantal beats blijft gelijk aan de
    huidige analyse. mode 'dry_run' (standaard) of 'execute'."""
    if mode not in ("dry_run", "execute"):
        raise ValueError("mode moet 'dry_run' of 'execute' zijn")
    with open_db() as db:
        content = resolve_track(db, track)
        plan = anlz.plan_beatgrid(db, content, float(bpm), anlz.parse_time(first_beat), first_beat_number)
        originals: dict[str, bytes] = {}

        def rollback() -> None:
            anlz.restore_anlz(originals)
            db.rollback()

        return safety.guarded_write("write_beatgrid", dry_run=(mode != "execute"), plan=plan,
                                    apply=lambda: anlz.apply_beatgrid(db, content, plan, originals),
                                    tracks=[track_label(content)], rollback=rollback)


# ======================================================================================
# Backups & log
# ======================================================================================


@_tool
def backup_now(label: str = "manual", include_anlz: bool = True) -> dict:
    """Maak nu een backup van master.db (+wal/shm), masterPlaylists6.xml, MySettings en de volledige
    ANLZ-map naar een tijdgestempelde map. Rekordbox's eigen 'Backup Library' is alleen via de GUI
    te starten; deze tool kopieert de bestanden zelf en verifieert de kopie (sha256)."""
    manifest = safety.create_backup(label, include_anlz=include_anlz)
    running = safety.is_rekordbox_running()
    safety.log_action("backup_now", "OK", backup_path=manifest["backup_path"],
                      details={"rekordbox_running_during_backup": running})
    if running:
        manifest["warning"] = ("Rekordbox draaide tijdens de backup; sluit Rekordbox voor een gegarandeerd "
                               "consistente kopie.")
    return manifest


@_tool
def list_backups(limit: int = 20) -> list:
    """Laatste backups (nieuwste eerst)."""
    return safety.list_backups()[:limit]


@_tool
def restore_backup(backup_path: str, restore_anlz: bool = True, dry_run: bool = True) -> dict:
    """Zet een backup terug (Rekordbox moet dicht zijn). Maakt eerst nog een backup van de huidige staat."""
    src = Path(backup_path)
    manifest = json.loads((src / "backup_manifest.json").read_text())
    plan = {"restore_from": str(src), "created": manifest.get("created"), "action_of_backup": manifest.get("action"),
            "restore_anlz": restore_anlz and bool(manifest.get("anlz_included"))}
    if dry_run:
        safety.log_action("restore_backup", "DRY-RUN", details=plan)
        return {"dry_run": True, "plan": plan}
    res = safety.restore_backup(src, restore_anlz=restore_anlz)
    safety.log_action("restore_backup", "OK", backup_path=res["safety_backup_of_previous_state"], details=res)
    return res


@_tool
def get_action_log(limit: int = 30) -> list:
    """Laatste regels van het schrijfactie-logboek."""
    return safety.read_action_log(limit)


def main() -> None:
    print(f"rekordbox-mcp: db={get_config().db_path}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
