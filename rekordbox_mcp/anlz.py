"""Cue points and beatgrids.

EXPERIMENTAL. Rekordbox's formats are reverse-engineered (pyrekordbox), not
officially documented. What this module does:

* Cue points are written as rows in the ``djmdCue`` table of master.db. That is
  what the Rekordbox collection shows. The ANLZ ``PCOB``/``PCO2`` cue tags (used
  for USB exports) are NOT rewritten: pyrekordbox has no builder for them and
  Rekordbox regenerates them on export.
* The mapping of hot cue letter -> ``djmdCue.Kind`` is not documented. We assume
  A,B,C,D,E,F,G,H -> 1,2,3,5,6,7,8,9 but *verify it against your own library*
  (tracks that already have hot cues in both djmdCue and ANLZ) before any
  execute. If the evidence contradicts or is missing, execute refuses unless
  ``force=True``.
* Rekordbox 6.6+/7 also has a ``contentCue`` table with JSON cues. If the
  target track has such a row, execute refuses unless ``force=True``.
* Beatgrids are written into the ANLZ ``.DAT`` (PQTZ) and ``.EXT`` (PQT2)
  files. Only grids with the existing number of beats can be rewritten.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from .db import bpm_from_db, bpm_to_db, tables, track_label

HOT_CUE_KIND = {"A": 1, "B": 2, "C": 3, "D": 5, "E": 6, "F": 7, "G": 8, "H": 9}
KIND_TO_LETTER = {v: k for k, v in HOT_CUE_KIND.items()}
LETTER_TO_ANLZ = {letter: i + 1 for i, letter in enumerate("ABCDEFGH")}  # ANLZ hot_cue: A=1 .. H=8

# Rekordbox cue colour palette (ColorTableIndex)
CUE_COLORS = {
    "pink": 1, "magenta": 3, "violet": 5, "purple": 7, "blue": 9, "navy": 11, "aqua": 13,
    "cyan": 15, "green": 17, "lime": 19, "yellow": 21, "orange": 23, "red": 25,
}


class CueFormatError(RuntimeError):
    pass


def parse_time(value: Any) -> float:
    """Seconds from 93.5, '93.5', '1:33.5' or '0:01:33.5'."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = s.split(":")
    if not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts) or len(parts) > 3:
        raise ValueError(f"Ongeldige tijd '{value}' (gebruik seconden of mm:ss.xxx)")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def _anlz_files(db: Any, content: Any) -> dict[Path, Any]:
    if not content.AnalysisDataPath:
        return {}
    root = db.get_anlz_dir(content)
    if not root.exists():
        return {}
    try:
        return db.read_anlz_files(content)
    except Exception:
        return {}


def anlz_cues(db: Any, content: Any) -> list[dict[str, Any]]:
    out = []
    for path, f in _anlz_files(db, content).items():
        for tagname in ("PCOB", "PCO2"):
            if tagname not in f:
                continue
            for tag in f.getall_tags(tagname):
                c = tag.content
                for e in c.entries:
                    out.append(
                        {
                            "file": path.name,
                            "tag": tagname,
                            "list": str(getattr(c, "cue_type", getattr(c, "type", ""))),
                            "hot_cue": int(e.hot_cue),
                            "time_ms": int(e.time),
                            "loop_end_ms": int(e.loop_time) if int(e.loop_time) not in (-1, 0xFFFFFFFF) else None,
                            "comment": getattr(e, "comment", None),
                        }
                    )
    return out


def db_cues(db: Any, content: Any) -> list[Any]:
    rows = db.query(tables.DjmdCue).filter_by(ContentID=str(content.ID)).all()
    return [r for r in rows if not getattr(r, "rb_local_deleted", 0)]


def cue_row_to_dict(r: Any) -> dict[str, Any]:
    kind = r.Kind or 0
    return {
        "id": str(r.ID),
        "kind": kind,
        "type": "memory" if kind == 0 else "hot",
        "slot_assumed": KIND_TO_LETTER.get(kind) if kind else None,
        "time_sec": round((r.InMsec or 0) / 1000, 3),
        "loop_end_sec": round(r.OutMsec / 1000, 3) if r.OutMsec not in (None, -1) else None,
        "color_index": r.ColorTableIndex,
        "comment": r.Comment,
    }


def content_cue_json(db: Any, content: Any) -> list[str]:
    rows = db.query(tables.ContentCue).filter_by(ContentID=str(content.ID)).all()
    return [r.Cues for r in rows if not getattr(r, "rb_local_deleted", 0)]


def inspect_cues(db: Any, content: Any) -> dict[str, Any]:
    rows = db_cues(db, content)
    return {
        "track": track_label(content),
        "db_cues_djmdCue": [cue_row_to_dict(r) for r in sorted(rows, key=lambda r: r.InMsec or 0)],
        "contentCue_json": content_cue_json(db, content),
        "anlz_cues": anlz_cues(db, content),
        "anlz_dir": str(db.get_anlz_dir(content)) if content.AnalysisDataPath else None,
        "beatgrid": beatgrid_summary(db, content),
    }


def infer_hot_cue_mapping(db: Any, max_tracks: int = 300) -> dict[str, Any]:
    """Compare djmdCue.Kind with ANLZ hot_cue numbers (matched on time) for
    tracks in *this* library, to confirm the Kind mapping before writing."""
    evidence: dict[int, dict[int, int]] = {}
    checked = 0
    q = db.query(tables.DjmdCue).filter(tables.DjmdCue.Kind > 0)
    content_ids = []
    for r in q.limit(5000):
        if r.ContentID not in content_ids:
            content_ids.append(r.ContentID)
    for cid in content_ids[:max_tracks]:
        content = db.get_content(ID=cid)
        if content is None:
            continue
        anlz = [c for c in anlz_cues(db, content) if c["hot_cue"] > 0]
        if not anlz:
            continue
        checked += 1
        for r in db_cues(db, content):
            if not r.Kind:
                continue
            match = [a for a in anlz if abs(a["time_ms"] - (r.InMsec or 0)) <= 2]
            if match:
                evidence.setdefault(int(r.Kind), {}).setdefault(match[0]["hot_cue"], 0)
                evidence[int(r.Kind)][match[0]["hot_cue"]] += 1
    observed = {k: max(v, key=v.get) for k, v in evidence.items()}
    contradictions = {}
    for letter, kind in HOT_CUE_KIND.items():
        if kind in observed and observed[kind] != LETTER_TO_ANLZ[letter]:
            contradictions[letter] = {"assumed_kind": kind, "observed_anlz_slot": observed[kind]}
    return {
        "tracks_compared": checked,
        "observed_kind_to_anlz_slot": observed,
        "contradictions": contradictions,
        "confirmed": bool(observed) and not contradictions,
    }


# --------------------------------------------------------------------------------------
# Writing cues
# --------------------------------------------------------------------------------------


def normalize_cues(cues: list[dict[str, Any]], length_sec: float | None) -> list[dict[str, Any]]:
    out = []
    used_slots = set()
    for i, c in enumerate(cues):
        ctype = str(c.get("type", "hot" if c.get("slot") else "memory")).lower()
        if ctype not in ("hot", "memory"):
            raise ValueError(f"cue {i}: type moet 'hot' of 'memory' zijn")
        t = parse_time(c.get("time", c.get("time_sec")))
        if t < 0 or (length_sec and t > length_sec):
            raise ValueError(f"cue {i}: tijd {t}s valt buiten de track (lengte {length_sec}s)")
        end = c.get("loop_end", c.get("end"))
        end_s = parse_time(end) if end not in (None, "") else None
        if end_s is not None and end_s <= t:
            raise ValueError(f"cue {i}: loop_end moet na de starttijd liggen")
        slot = None
        if ctype == "hot":
            slot = str(c.get("slot", "")).upper()
            if slot not in HOT_CUE_KIND:
                raise ValueError(f"cue {i}: hot cue slot moet A-H zijn")
            if slot in used_slots:
                raise ValueError(f"cue {i}: slot {slot} dubbel opgegeven")
            used_slots.add(slot)
        color = c.get("color")
        color_idx = None
        if color is not None:
            color_idx = CUE_COLORS.get(str(color).lower()) if not str(color).isdigit() else int(color)
            if color_idx is None:
                raise ValueError(f"cue {i}: onbekende kleur '{color}'. Kies uit {sorted(CUE_COLORS)}")
        out.append({"type": ctype, "slot": slot, "time_sec": round(t, 3), "loop_end_sec": end_s,
                    "comment": c.get("comment") or "", "color_index": color_idx})
    return out


def plan_cues(db: Any, content: Any, cues: list[dict[str, Any]], replace_existing: bool) -> dict[str, Any]:
    norm = normalize_cues(cues, content.Length)
    existing = [cue_row_to_dict(r) for r in db_cues(db, content)]
    to_remove = []
    conflicts = []
    for n in norm:
        for e in existing:
            same_slot = n["type"] == "hot" and e["type"] == "hot" and e["kind"] == HOT_CUE_KIND[n["slot"]]
            same_mem = n["type"] == "memory" and e["type"] == "memory" and abs(e["time_sec"] - n["time_sec"]) < 0.01
            if same_slot or same_mem:
                (to_remove if replace_existing else conflicts).append(e)
    rows = []
    for n in norm:
        ms = int(round(n["time_sec"] * 1000))
        rows.append({
            "Kind": 0 if n["type"] == "memory" else HOT_CUE_KIND[n["slot"]],
            "InMsec": ms,
            "InFrame": int(ms * 150 / 1000),
            "OutMsec": int(round(n["loop_end_sec"] * 1000)) if n["loop_end_sec"] is not None else -1,
            "OutFrame": int(n["loop_end_sec"] * 150) if n["loop_end_sec"] is not None else 0,
            "ColorTableIndex": n["color_index"] if n["color_index"] is not None else 0,
            "Comment": n["comment"],
            "_label": f"{'Hot ' + n['slot'] if n['slot'] else 'Memory'} @ {n['time_sec']}s"
                      + (f" loop→{n['loop_end_sec']}s" if n["loop_end_sec"] is not None else ""),
        })
    warnings = []
    if content_cue_json(db, content):
        warnings.append("Deze track heeft een contentCue-rij (RB 6.6+/7 JSON-cues). Rekordbox kan die als bron "
                        "gebruiken en onze djmdCue-rijen negeren/overschrijven. Execute vereist force=True.")
    return {
        "track": track_label(content),
        "file_mpeg_note": "InMpegFrame/InMpegAbs worden 0 gezet (correct voor CBR/lossless; VBR-mp3 kan een paar ms afwijken).",
        "new_cues": rows,
        "existing_cues": existing,
        "will_remove": to_remove,
        "conflicts": conflicts,
        "warnings": warnings,
    }


def apply_cues(db: Any, content: Any, plan: dict[str, Any]) -> dict[str, Any]:
    if plan["conflicts"]:
        raise CueFormatError(f"Bestaande cues op dezelfde slots/tijden: {plan['conflicts']}. Gebruik replace_existing=true.")
    from . import cloud

    mode, _why = cloud.delete_mode(db, "djmdCue")
    removed = 0
    for e in plan["will_remove"]:
        row = db.query(tables.DjmdCue).filter_by(ID=e["id"]).one()
        cloud.remove(db, row, mode)
        removed += 1
    created = []
    for r in plan["new_cues"]:
        cue_id = db.generate_unused_id(tables.DjmdCue)
        row = tables.DjmdCue.create(
            ID=str(cue_id),
            ContentID=str(content.ID),
            InMsec=r["InMsec"], InFrame=r["InFrame"], InMpegFrame=0, InMpegAbs=0,
            OutMsec=r["OutMsec"], OutFrame=r["OutFrame"], OutMpegFrame=0, OutMpegAbs=0,
            Kind=r["Kind"],
            Color=-1,
            ColorTableIndex=r["ColorTableIndex"],
            ActiveLoop=0,
            Comment=r["Comment"],
            BeatLoopSize=0,
            CueMicrosec=r["InMsec"] * 1000,
            InPointSeekInfo="",
            OutPointSeekInfo="",
            ContentUUID=content.UUID,
            UUID=str(uuid.uuid4()),
        )
        db.add(row)
        created.append({"id": str(cue_id), "cue": r["_label"]})
    db.flush()
    db.commit()
    return {"removed": removed, "created": created}


# --------------------------------------------------------------------------------------
# Beatgrid
# --------------------------------------------------------------------------------------


def beatgrid_summary(db: Any, content: Any) -> dict[str, Any] | None:
    for path, f in _anlz_files(db, content).items():
        if "PQTZ" in f:
            tag = f.get_tag("PQTZ")
            beats, bpms, times = tag.get()
            if len(times) == 0:
                return {"file": path.name, "beats": 0}
            return {
                "file": path.name,
                "beats": int(len(times)),
                "first_beat_sec": float(times[0]),
                "first_beat_number": int(beats[0]),
                "bpm_first": float(bpms[0]),
                "bpm_unique": sorted({float(b) for b in bpms})[:10],
                "db_bpm": bpm_from_db(content.BPM),
            }
    return None


def _grid(n: int, bpm: float, first: float, first_beat_number: int) -> tuple[list[int], list[float], list[float]]:
    step = 60.0 / bpm
    times = [first + i * step for i in range(n)]
    beats = [((first_beat_number - 1 + i) % 4) + 1 for i in range(n)]
    return beats, [bpm] * n, times


def plan_beatgrid(db: Any, content: Any, bpm: float, first_beat_sec: float, first_beat_number: int) -> dict[str, Any]:
    if not 40 <= bpm <= 250:
        raise ValueError("BPM moet tussen 40 en 250 liggen")
    if first_beat_number not in (1, 2, 3, 4):
        raise ValueError("first_beat_number moet 1-4 zijn")
    files = _anlz_files(db, content)
    if not files:
        raise CueFormatError("Geen ANLZ-bestanden gevonden voor deze track (niet geanalyseerd?)")
    targets = []
    for path, f in files.items():
        for tag in ("PQTZ", "PQT2"):
            if tag in f:
                t = f.get_tag(tag)
                n = len(t.content.entries) if tag == "PQTZ" else int(t.content.entry_count)
                targets.append({"file": str(path), "tag": tag, "beats": n})
    if not any(t["tag"] == "PQTZ" for t in targets):
        raise CueFormatError("Geen PQTZ-beatgrid in de .DAT gevonden")
    current = beatgrid_summary(db, content)
    n = current["beats"] if current else 0
    beats, _bpms, times = _grid(n, bpm, first_beat_sec, first_beat_number)
    length = float(content.Length or 0)
    return {
        "track": track_label(content),
        "current": current,
        "new": {"bpm": bpm, "first_beat_sec": first_beat_sec, "first_beat_number": first_beat_number,
                "beats": n, "last_beat_sec": round(times[-1], 3) if times else None},
        "db_bpm_change": {"old": bpm_from_db(content.BPM), "new": round(bpm, 2), "stored_as": bpm_to_db(bpm)},
        "anlz_targets": targets,
        "warnings": (["Laatste beat valt voorbij het einde van de track"] if times and length and times[-1] > length + 1 else []),
    }


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def apply_beatgrid(db: Any, content: Any, plan: dict[str, Any], originals: dict[str, bytes]) -> dict[str, Any]:
    from pyrekordbox.anlz import AnlzFile

    new = plan["new"]
    written = []
    for path, f in _anlz_files(db, content).items():
        touched = False
        if "PQTZ" in f:
            tag = f.get_tag("PQTZ")
            n = len(tag.content.entries)
            beats, bpms, times = _grid(n, new["bpm"], new["first_beat_sec"], new["first_beat_number"])
            tag.set(beats, bpms, times)
            touched = True
        if "PQT2" in f:
            tag = f.get_tag("PQT2")
            n = int(tag.content.entry_count)
            if n > 0:
                beats, _b, times = _grid(n, new["bpm"], new["first_beat_sec"], new["first_beat_number"])
                anchors = tag.content.bpm
                # the two anchor entries are the first and the last beat; set fields
                # directly (pyrekordbox PQT2.set_bpms writes to a non-existent field)
                for anchor, idx in ((anchors[0], 0), (anchors[1], n - 1)):
                    anchor.beat = beats[idx]
                    anchor.tempo = bpm_to_db(new["bpm"])
                    anchor.time = int(round(times[idx] * 1000))
                touched = True
        if not touched:
            continue
        data = f.build()
        check = AnlzFile.parse(data)  # verify it round-trips before touching disk
        if "PQTZ" in check:
            got = check.get_tag("PQTZ").get()[2]
            if len(got) and abs(float(got[0]) - new["first_beat_sec"]) > 0.002:
                raise CueFormatError(f"Verificatie van {path.name} mislukt")
        originals[str(path)] = Path(path).read_bytes()
        tmp = Path(str(path) + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        _update_content_file(db, content, Path(path), originals[str(path)], data)
        written.append(Path(path).name)
    content.BPM = bpm_to_db(new["bpm"])
    db.commit()
    return {"anlz_files_written": written, "db_bpm": bpm_from_db(content.BPM)}


def _update_content_file(db: Any, content: Any, path: Path, old: bytes, new: bytes) -> None:
    """contentFile keeps md5 + size of ANLZ files; keep them consistent, but only
    when we can confirm the stored hash really is md5(file)."""
    for row in db.query(tables.ContentFile).filter_by(ContentID=str(content.ID)).all():
        if row.Path and row.Path.replace("\\", "/").endswith(path.name) and str(path.parent.name) in row.Path:
            if row.Hash == _md5(old):
                row.Hash = _md5(new)
                row.Size = len(new)


def restore_anlz(originals: dict[str, bytes]) -> None:
    for p, data in originals.items():
        Path(p).write_bytes(data)
