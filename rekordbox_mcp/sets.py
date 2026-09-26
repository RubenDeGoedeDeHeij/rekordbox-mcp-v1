"""Build DJ sets: select tracks by names or criteria and put them in play order."""

from __future__ import annotations

from typing import Any

from .db import bpm_from_db, file_exists, to_camelot, track_energy

ORDERINGS = ("as_given", "bpm_ascending", "bpm_descending", "energy_ramp", "warmup_peak_cooldown", "harmonic")


def _camelot_parts(code: str | None) -> tuple[int, str] | None:
    if not code:
        return None
    return int(code[:-1]), code[-1]


def camelot_distance(a: str | None, b: str | None) -> int:
    """0 = same key, 1 = compatible neighbour (±1 or relative maj/min), higher = worse."""
    pa, pb = _camelot_parts(a), _camelot_parts(b)
    if pa is None or pb is None:
        return 3
    (na, la), (nb, lb) = pa, pb
    step = min((na - nb) % 12, (nb - na) % 12)
    if la == lb:
        return step
    return step + 1  # switching A<->B at the same number is a compatible move


def _energy_or_default(t: Any) -> float:
    e = track_energy(t)
    if e is not None:
        return float(e)
    # fall back to rating (0-5 stars -> 0-10) so ordering still does something sensible
    return float((t.Rating or 0) * 2) if t.Rating else 5.0


def order_tracks(tracks: list[Any], ordering: str) -> list[Any]:
    if ordering == "as_given":
        return list(tracks)
    bpm = lambda t: bpm_from_db(t.BPM) or 0.0  # noqa: E731
    if ordering == "bpm_ascending":
        return sorted(tracks, key=bpm)
    if ordering == "bpm_descending":
        return sorted(tracks, key=bpm, reverse=True)
    if ordering == "energy_ramp":
        return sorted(tracks, key=lambda t: (_energy_or_default(t), bpm(t)))
    if ordering == "warmup_peak_cooldown":
        ranked = sorted(tracks, key=lambda t: (_energy_or_default(t), bpm(t)))
        n = len(ranked)
        # ~70% build up, peak, ~30% cool down: peak ends up about two thirds in
        up, down = [], []
        for i, t in enumerate(ranked):
            (up if i % 10 < 7 or i == n - 1 else down).append(t)
        return up + list(reversed(down))
    if ordering == "harmonic":
        return _harmonic_chain(tracks)
    raise ValueError(f"Onbekende ordering '{ordering}'. Kies uit {ORDERINGS}")


def _harmonic_chain(tracks: list[Any]) -> list[Any]:
    """Greedy chain: start at the slowest track, then always pick the most
    key-compatible next track, tie-breaking on the smallest BPM jump."""
    if not tracks:
        return []
    remaining = sorted(tracks, key=lambda t: bpm_from_db(t.BPM) or 0.0)
    chain = [remaining.pop(0)]
    while remaining:
        cur = chain[-1]
        cur_key = to_camelot(cur.KeyName)
        cur_bpm = bpm_from_db(cur.BPM) or 0.0
        best = min(
            remaining,
            key=lambda t: (
                camelot_distance(cur_key, to_camelot(t.KeyName)),
                abs((bpm_from_db(t.BPM) or 0.0) - cur_bpm),
            ),
        )
        remaining.remove(best)
        chain.append(best)
    return chain


def limit_tracks(tracks: list[Any], max_tracks: int | None, target_minutes: float | None) -> list[Any]:
    out = []
    total = 0.0
    for t in tracks:
        if max_tracks is not None and len(out) >= max_tracks:
            break
        if target_minutes is not None and total >= target_minutes * 60:
            break
        out.append(t)
        total += float(t.Length or 0)
    return out


def describe_transitions(tracks: list[Any]) -> list[dict[str, Any]]:
    rows = []
    prev = None
    for i, t in enumerate(tracks, 1):
        cam = to_camelot(t.KeyName)
        row = {
            "pos": i,
            "id": str(t.ID),
            "track": f"{t.ArtistName or '?'} - {t.Title}",
            "bpm": bpm_from_db(t.BPM),
            "key": cam or t.KeyName,
            "energy": track_energy(t),
            "genre": t.GenreName,
            "file_exists": file_exists(t.FolderPath),
        }
        if prev is not None:
            pb = bpm_from_db(prev.BPM) or 0.0
            cb = bpm_from_db(t.BPM) or 0.0
            row["bpm_jump"] = round(cb - pb, 2)
            row["key_distance"] = camelot_distance(to_camelot(prev.KeyName), cam)
        rows.append(row)
        prev = t
    return rows
