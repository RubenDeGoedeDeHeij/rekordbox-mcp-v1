"""Rekordbox MySetting files (MYSETTING.DAT, MYSETTING2.DAT, DJMMYSETTING.DAT, DEVSETTING.DAT).

These binary files hold the player/mixer preferences that Rekordbox exports to
USB and to connected hardware (quantize, auto cue, jog display, crossfader
curve, ...). pyrekordbox parses and rebuilds them including the checksum.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

from .config import Config, get_config

FILE_NAMES = ("MYSETTING.DAT", "MYSETTING2.DAT", "DJMMYSETTING.DAT", "DEVSETTING.DAT")


def find_setting_files(cfg: Config | None = None) -> dict[str, Path]:
    """Locate the MySetting files: first via the database (djmdSettingFile),
    then by searching the Rekordbox directories."""
    cfg = cfg or get_config()
    found: dict[str, Path] = {}
    try:
        from .db import open_db

        with open_db(cfg) as db:
            for p in db.get_mysetting_paths():
                if p.exists():
                    found.setdefault(p.name.upper(), p)
    except Exception:
        pass

    roots = []
    if cfg.db_path is not None:
        roots.append(cfg.db_dir)
    if platform.system() == "Darwin":
        roots += [
            Path.home() / "Library" / "Application Support" / "Pioneer" / "rekordbox7",
            Path.home() / "Library" / "Application Support" / "Pioneer" / "rekordbox6",
            Path.home() / "Library" / "Pioneer" / "rekordbox",
        ]
    for root in roots:
        if not root.exists():
            continue
        for name in FILE_NAMES:
            if name in found:
                continue
            for cand in [root / name, root / "PIONEER" / name]:
                if cand.exists():
                    found[name] = cand
                    break
    return found


def _options_for(file_obj: Any) -> dict[str, list[str]]:
    """Allowed values per key, read from the construct Enum definitions."""
    body = None
    for sc in file_obj.struct.subcons:
        if sc.name == "data":
            body = sc.subcon
    opts: dict[str, list[str]] = {}
    if body is None:
        return opts
    for sc in getattr(body, "subcons", []):
        sub = sc.subcon
        while not hasattr(sub, "encmapping") and hasattr(sub, "subcon"):
            sub = sub.subcon
        if hasattr(sub, "encmapping"):
            opts[sc.name] = sorted(str(k) for k in sub.encmapping.keys())
    return opts


def read_settings(cfg: Config | None = None, file: str | None = None) -> dict[str, Any]:
    from pyrekordbox.mysettings import read_mysetting_file

    files = find_setting_files(cfg)
    out: dict[str, Any] = {}
    for name, path in sorted(files.items()):
        if file and name.upper() != file.upper() and name.upper() != file.upper() + ".DAT":
            continue
        try:
            obj = read_mysetting_file(path)
            opts = _options_for(obj)
            out[name] = {
                "path": str(path),
                "values": {k: obj.get(k) for k in obj.keys() if not k.startswith("u")},
                "options": opts,
            }
        except Exception as exc:
            out[name] = {"path": str(path), "error": repr(exc)}
    return out


def plan_setting_change(key: str, value: str, file: str | None, cfg: Config | None = None) -> dict[str, Any]:
    from pyrekordbox.mysettings import read_mysetting_file

    files = find_setting_files(cfg)
    candidates = []
    for name, path in files.items():
        if file and name.upper() not in (file.upper(), file.upper() + ".DAT"):
            continue
        obj = read_mysetting_file(path)
        if key in obj.defaults:
            candidates.append((name, path, obj))
    if not candidates:
        raise KeyError(f"Setting '{key}' niet gevonden in {sorted(files) or 'geen MySetting-bestanden'}")
    if len(candidates) > 1:
        raise KeyError(f"Setting '{key}' bestaat in meerdere bestanden {[c[0] for c in candidates]}; geef 'file' mee")
    name, path, obj = candidates[0]
    opts = _options_for(obj).get(key)
    if opts is not None and value not in opts:
        raise ValueError(f"Ongeldige waarde '{value}' voor {key}. Toegestaan: {opts}")
    return {"file": name, "path": str(path), "key": key, "old_value": obj.get(key), "new_value": value}


def apply_setting_change(plan: dict[str, Any]) -> dict[str, Any]:
    from pyrekordbox.mysettings import read_mysetting_file

    path = Path(plan["path"])
    obj = read_mysetting_file(path)
    obj.set(plan["key"], plan["new_value"])
    data = obj.build()  # raises if the value cannot be encoded — before touching the file
    check = type(obj).parse(data)
    if str(check.get(plan["key"])) != str(plan["new_value"]):
        raise RuntimeError("Verificatie mislukt; origineel ongewijzigd")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)  # atomic on the same filesystem
    return {"verified_value": read_mysetting_file(path).get(plan["key"])}
