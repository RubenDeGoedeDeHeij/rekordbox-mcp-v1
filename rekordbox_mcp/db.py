"""Database access helpers on top of pyrekordbox.

Pitfalls handled here:
* BPM is stored as integer * 100 (13000 == 130.00 BPM).
* Rows with ``rb_local_deleted == 1`` are soft-deleted and must be ignored.
* ``FolderPath`` may point to a file that no longer exists locally (moved to
  iCloud, external drive unplugged ...). Every serialized track has ``exists``.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import re
from pathlib import Path
from typing import Any, Iterator

from .config import Config, get_config

# pyrekordbox logs to stderr (never stdout, which is the MCP channel); keep it quiet.
logging.getLogger("pyrekordbox").setLevel(logging.ERROR)

try:  # pyrekordbox >= 0.5 renamed the class
    from pyrekordbox import MasterDatabase as _Database  # type: ignore
except ImportError:  # pragma: no cover - depends on installed version
    from pyrekordbox import Rekordbox6Database as _Database  # type: ignore

try:
    from pyrekordbox.db6 import tables  # type: ignore
except ImportError:  # pragma: no cover
    from pyrekordbox.masterdb import models as tables  # type: ignore


def bpm_from_db(value: Any) -> float | None:
    if value in (None, 0):
        return None
    return round(int(value) / 100.0, 2)


def bpm_to_db(bpm: float) -> int:
    return int(round(float(bpm) * 100))


@contextlib.contextmanager
def open_db(cfg: Config | None = None) -> Iterator[Any]:
    """Open master.db for the duration of one tool call."""
    cfg = cfg or get_config()
    if cfg.db_path is None or not cfg.db_path.exists():
        raise FileNotFoundError(
            f"Rekordbox database niet gevonden op '{cfg.db_path}'. Zet RBMCP_DB_PATH naar master.db."
        )
    kwargs: dict[str, Any] = {"path": cfg.db_path, "db_dir": cfg.db_path.parent}
    if cfg.db_key:
        kwargs["key"] = cfg.db_key
    db = _Database(**kwargs)
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass


def active(rows: Any) -> list[Any]:
    return [r for r in rows if not getattr(r, "rb_local_deleted", 0)]


def all_tracks(db: Any) -> list[Any]:
    return active(db.get_content().all())


def file_exists(path: str | None) -> bool:
    if not path:
        return False
    try:
        return Path(path).exists()
    except OSError:
        return False


ENERGY_RE = re.compile(r"energy\s*[:\-]?\s*(\d{1,2})", re.I)
CAMELOT_RE = re.compile(r"^\s*(1[0-2]|[1-9])\s*([ABab])\s*$")

# Musical key -> Camelot code
_KEY_TO_CAMELOT = {
    "Abm": "1A", "G#m": "1A", "B": "1B",
    "Ebm": "2A", "D#m": "2A", "F#": "2B", "Gb": "2B",
    "Bbm": "3A", "A#m": "3A", "Db": "3B", "C#": "3B",
    "Fm": "4A", "Ab": "4B", "G#": "4B",
    "Cm": "5A", "Eb": "5B", "D#": "5B",
    "Gm": "6A", "Bb": "6B", "A#": "6B",
    "Dm": "7A", "F": "7B",
    "Am": "8A", "C": "8B",
    "Em": "9A", "G": "9B",
    "Bm": "10A", "D": "10B",
    "F#m": "11A", "Gbm": "11A", "A": "11B",
    "Dbm": "12A", "C#m": "12A", "E": "12B",
}


def to_camelot(key: str | None) -> str | None:
    if not key:
        return None
    key = key.strip()
    m = CAMELOT_RE.match(key)
    if m:
        return f"{int(m.group(1))}{m.group(2).upper()}"
    norm = key.replace("min", "m").replace("maj", "").replace(" ", "")
    return _KEY_TO_CAMELOT.get(norm)


def track_energy(content: Any) -> int | None:
    """Energy level 1-10 from the comment field (Mixed In Key style 'Energy 7')."""
    for text in (getattr(content, "Commnt", None), getattr(content, "Comment", None)):
        if text:
            m = ENERGY_RE.search(str(text))
            if m:
                return int(m.group(1))
    return None


def _safe(obj: Any, attr: str) -> Any:
    try:
        return getattr(obj, attr)
    except Exception:
        return None


def track_to_dict(c: Any, verbose: bool = False) -> dict[str, Any]:
    from .cloud import file_state, in_cloud_storage

    key = _safe(c, "KeyName")
    d = {
        "id": str(c.ID),
        "title": c.Title,
        "artist": _safe(c, "ArtistName"),
        "genre": _safe(c, "GenreName"),
        "bpm": bpm_from_db(c.BPM),
        "key": key,
        "camelot": to_camelot(key),
        "energy": track_energy(c),
        "rating": c.Rating,
        "length_sec": c.Length,
        "path": c.FolderPath,
        "exists": file_exists(c.FolderPath),
        "file_state": file_state(c.FolderPath),  # local | online_only (cloud placeholder) | missing
        "in_cloud_storage": in_cloud_storage(c.FolderPath),
    }
    if verbose:
        d.update(
            {
                "album": _safe(c, "AlbumName"),
                "label": _safe(c, "LabelName"),
                "bitrate": c.BitRate,
                "file_size": c.FileSize,
                "file_type": c.FileType,
                "comment": c.Commnt,
                "play_count": c.DJPlayCount,
                "date_added": str(c.created_at) if _safe(c, "created_at") else None,
                "analysis_path": c.AnalysisDataPath,
            }
        )
    return d


def track_label(c: Any) -> str:
    artist = _safe(c, "ArtistName")
    return f"{c.ID}: {artist + ' - ' if artist else ''}{c.Title}"


def _norm(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"\((original|extended)( mix)?\)", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


class TrackResolutionError(ValueError):
    def __init__(self, query: str, candidates: list[str]):
        self.query = query
        self.candidates = candidates
        hint = f" Kandidaten: {candidates}" if candidates else ""
        super().__init__(f"Track '{query}' niet eenduidig gevonden.{hint}")


def resolve_track(db: Any, query: str | int, tracks: list[Any] | None = None) -> Any:
    """Find exactly one track by ID, 'Artist - Title', title or file name."""
    tracks = tracks if tracks is not None else all_tracks(db)
    q = str(query).strip()
    if q.isdigit():
        for t in tracks:
            if str(t.ID) == q:
                return t
    nq = _norm(q)

    def labels(t: Any) -> list[str]:
        artist = _safe(t, "ArtistName") or ""
        out = [_norm(t.Title), _norm(f"{artist} - {t.Title}"), _norm(f"{t.Title} - {artist}")]
        if t.FolderPath:
            out.append(_norm(Path(t.FolderPath).stem))
        return out

    exact = [t for t in tracks if nq in labels(t)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # prefer the copies whose file exists
        existing = [t for t in exact if file_exists(t.FolderPath)]
        if len(existing) == 1:
            return existing[0]
        raise TrackResolutionError(q, [track_label(t) for t in exact[:10]])

    contains = [t for t in tracks if any(nq and nq in lbl for lbl in labels(t))]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        raise TrackResolutionError(q, [track_label(t) for t in contains[:10]])

    scored = []
    for t in tracks:
        score = max(difflib.SequenceMatcher(None, nq, lbl).ratio() for lbl in labels(t))
        scored.append((score, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] >= 0.85 and (len(scored) == 1 or scored[0][0] - scored[1][0] > 0.05):
        return scored[0][1]
    raise TrackResolutionError(q, [track_label(t) for s, t in scored[:5] if s > 0.4])


def resolve_tracks(db: Any, queries: list[str | int]) -> tuple[list[Any], list[dict[str, Any]]]:
    tracks = all_tracks(db)
    found, errors = [], []
    for q in queries:
        try:
            found.append(resolve_track(db, q, tracks))
        except TrackResolutionError as exc:
            errors.append({"query": str(q), "error": str(exc), "candidates": exc.candidates})
    return found, errors


def search_tracks(
    db: Any,
    text: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    genres: list[str] | None = None,
    key: str | None = None,
    energy_min: int | None = None,
    energy_max: int | None = None,
    rating_min: int | None = None,
    only_existing: bool = False,
    only_local: bool = False,
) -> list[Any]:
    genres_l = {g.lower() for g in genres} if genres else None
    want_camelot = to_camelot(key) if key else None
    ntext = _norm(text) if text else None
    out = []
    for c in all_tracks(db):
        bpm = bpm_from_db(c.BPM)
        if bpm_min is not None and (bpm is None or bpm < bpm_min):
            continue
        if bpm_max is not None and (bpm is None or bpm > bpm_max):
            continue
        if genres_l is not None:
            g = (_safe(c, "GenreName") or "").lower()
            if not any(want == g or want in g for want in genres_l):
                continue
        if want_camelot and to_camelot(_safe(c, "KeyName")) != want_camelot:
            continue
        if energy_min is not None or energy_max is not None:
            e = track_energy(c)
            if e is None or (energy_min is not None and e < energy_min) or (energy_max is not None and e > energy_max):
                continue
        if rating_min is not None and (c.Rating or 0) < rating_min:
            continue
        if ntext:
            hay = _norm(" ".join(filter(None, [c.Title, _safe(c, "ArtistName"), _safe(c, "AlbumName"), _safe(c, "GenreName"), c.FolderPath])))
            if not all(w in hay for w in ntext.split()):
                continue
        if only_existing and not file_exists(c.FolderPath):
            continue
        if only_local:
            from .cloud import file_state

            if file_state(c.FolderPath) != "local":
                continue
        out.append(c)
    return out
