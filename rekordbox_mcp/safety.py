"""Safety layer: Rekordbox process check, mandatory backups and the action log.

Every tool that modifies the Rekordbox database, the ANLZ analysis files or the
MySetting files goes through :func:`guarded_write`. That function

1. refuses to run while Rekordbox is open (SQLite lock / corruption risk),
2. makes a timestamped backup of master.db + ANLZ directory + settings files
   and verifies it on disk *before* anything is written,
3. runs the actual write,
4. logs the action (timestamp, action, tracks, backup path, result).

In dry-run mode only step 4 happens (flagged as DRY-RUN) and nothing is written.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import Config, get_config

REKORDBOX_PROCESS_NAMES = {"rekordbox", "rekordbox.exe"}


class RekordboxRunningError(RuntimeError):
    pass


class BackupError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Process check
# --------------------------------------------------------------------------------------


def find_processes(names: set[str]) -> list[dict[str, Any]]:
    """Running processes whose name (or executable name) is in ``names`` (lowercase)."""
    found: dict[int, dict[str, Any]] = {}
    try:
        import psutil

        for proc in psutil.process_iter(["pid", "name", "exe"]):
            name = (proc.info.get("name") or "").lower()
            exe_name = os.path.basename((proc.info.get("exe") or "").lower())
            if name in names or exe_name in names:
                found[proc.info["pid"]] = {"pid": proc.info["pid"], "name": proc.info.get("name"), "exe": proc.info.get("exe")}
    except Exception:  # psutil missing or access denied: fall back to pgrep
        pass

    if platform.system() != "Windows" and shutil.which("pgrep"):
        for name in names:
            if name.endswith(".exe"):
                continue
            try:
                out = subprocess.run(["pgrep", "-x", "-i", name], capture_output=True, text=True, timeout=5)
                for line in out.stdout.split():
                    if line.strip().isdigit():
                        pid = int(line)
                        found.setdefault(pid, {"pid": pid, "name": name, "exe": None})
            except Exception:
                pass
    return list(found.values())


def find_rekordbox_processes() -> list[dict[str, Any]]:
    """Return running Rekordbox *application* processes.

    The background ``rekordboxAgent`` (cloud sync helper) is ignored: it keeps
    running after the app is closed and does not hold the database.
    """
    return find_processes(REKORDBOX_PROCESS_NAMES)


def is_rekordbox_running() -> bool:
    return bool(find_rekordbox_processes())


def ensure_rekordbox_closed() -> None:
    procs = find_rekordbox_processes()
    if procs:
        pids = ", ".join(str(p["pid"]) for p in procs)
        raise RekordboxRunningError(
            f"Rekordbox is nog open (PID {pids}). Sluit Rekordbox volledig af (Cmd+Q) "
            "voordat je schrijfacties uitvoert — de database is anders gelockt en kan "
            "corrupt raken. Er is NIETS geschreven."
        )


# --------------------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _clone_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        # APFS copy-on-write clone: instant and takes no extra space until changed.
        res = subprocess.run(["cp", "-c", "-p", str(src), str(dst)], capture_output=True)
        if res.returncode == 0:
            return
    shutil.copy2(src, dst)


def _clone_tree(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        res = subprocess.run(["cp", "-c", "-R", "-p", str(src), str(dst)], capture_output=True)
        if res.returncode == 0:
            return
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def _count_files(root: Path) -> tuple[int, int]:
    n = size = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            n += 1
            try:
                size += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return n, size


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:40] or "backup"


def _extra_setting_files(cfg: Config) -> list[Path]:
    """MySetting files that live outside the top level of the db directory."""
    from .settings import find_setting_files  # local import, avoids a cycle

    try:
        return [p for p in find_setting_files(cfg).values() if p.exists()]
    except Exception:
        return []


def create_backup(action: str = "manual", cfg: Config | None = None, include_anlz: bool = True) -> dict[str, Any]:
    """Copy master.db (+ wal/shm), masterPlaylists6.xml, settings and the ANLZ dir.

    Returns the backup manifest. Raises BackupError when the copy cannot be
    verified — callers must then abort the write.

    Rekordbox's own "Backup Library" (File > Library > Backup Library) is GUI-only;
    there is no CLI flag, URL scheme or AppleScript dictionary to trigger it, so
    the copy is done here.
    """
    cfg = cfg or get_config()
    db_path = cfg.db_path
    if db_path is None or not db_path.exists():
        raise BackupError(f"master.db niet gevonden op {db_path}")
    db_dir = db_path.parent

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = cfg.backup_dir / f"{stamp}_{_slug(action)}"
    target.mkdir(parents=True, exist_ok=False)

    copied: list[str] = []
    try:
        # 1) top-level database files (skip Rekordbox's own rotating backups)
        for item in sorted(db_dir.iterdir()):
            if not item.is_file():
                continue
            low = item.name.lower()
            if "backup" in low:
                continue
            if low.startswith("master.db") or low.endswith((".xml", ".dat", ".db", ".json", ".edb")):
                _clone_file(item, target / item.name)
                copied.append(item.name)

        # 2) MySetting files stored elsewhere (e.g. db_dir/PIONEER/...)
        for p in _extra_setting_files(cfg):
            try:
                rel = p.relative_to(db_dir)
            except ValueError:
                rel = Path("_external_settings") / p.name
            dst = target / rel
            if not dst.exists():
                _clone_file(p, dst)
                copied.append(str(rel))

        # 3) ANLZ directory (cues / beatgrids / waveforms)
        anlz_info = None
        if include_anlz and cfg.anlz_dir.exists():
            rel = cfg.anlz_dir.relative_to(db_dir)
            _clone_tree(cfg.anlz_dir, target / rel)
            n_src, size_src = _count_files(cfg.anlz_dir)
            n_dst, size_dst = _count_files(target / rel)
            if n_src != n_dst or size_src != size_dst:
                raise BackupError(f"ANLZ-backup onvolledig: {n_dst}/{n_src} bestanden, {size_dst}/{size_src} bytes")
            anlz_info = {"path": str(rel), "files": n_dst, "bytes": size_dst}
            copied.append(str(rel) + "/")

        # 4) verify master.db
        src_hash = _sha256(db_path)
        dst_hash = _sha256(target / db_path.name)
        if src_hash != dst_hash:
            raise BackupError("master.db backup checksum komt niet overeen met origineel")

        manifest = {
            "backup_path": str(target),
            "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "source_db": str(db_path),
            "master_db_sha256": dst_hash,
            "master_db_bytes": (target / db_path.name).stat().st_size,
            "anlz": anlz_info,
            "anlz_included": anlz_info is not None,
            "copied": copied,
            "restore_hint": (
                "Sluit Rekordbox, kopieer master.db (en eventueel share/PIONEER/USBANLZ) uit deze map "
                "terug naar " + str(db_dir) + " — of gebruik de tool restore_backup."
            ),
        }
        (target / "backup_manifest.json").write_text(json.dumps(manifest, indent=2))
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"Backup mislukt: {exc}") from exc

    _prune_backups(cfg)
    return manifest


def _prune_backups(cfg: Config) -> None:
    if cfg.backup_keep <= 0 or not cfg.backup_dir.exists():
        return
    backups = sorted(p for p in cfg.backup_dir.iterdir() if (p / "backup_manifest.json").exists())
    for old in backups[: -cfg.backup_keep]:
        shutil.rmtree(old, ignore_errors=True)


def list_backups(cfg: Config | None = None) -> list[dict[str, Any]]:
    cfg = cfg or get_config()
    out = []
    if not cfg.backup_dir.exists():
        return out
    for p in sorted(cfg.backup_dir.iterdir(), reverse=True):
        mf = p / "backup_manifest.json"
        if mf.exists():
            try:
                data = json.loads(mf.read_text())
            except Exception:
                data = {"backup_path": str(p)}
            out.append(data)
    return out


def restore_backup(backup_path: str | Path, cfg: Config | None = None, restore_anlz: bool = True) -> dict[str, Any]:
    """Copy a backup back into the Rekordbox directory (Rekordbox must be closed).

    A fresh safety backup of the *current* state is made first.
    """
    cfg = cfg or get_config()
    src = Path(backup_path)
    if not (src / "backup_manifest.json").exists():
        raise BackupError(f"{src} is geen geldige backupmap (backup_manifest.json ontbreekt)")
    ensure_rekordbox_closed()
    safety = create_backup("pre-restore", cfg)
    db_dir = cfg.db_dir
    restored = []
    for item in src.iterdir():
        if item.is_file() and item.name != "backup_manifest.json":
            shutil.copy2(item, db_dir / item.name)
            restored.append(item.name)
    # a restored master.db must not be combined with a stale WAL/SHM from after the backup
    for suffix in ("-wal", "-shm"):
        stale = db_dir / (cfg.db_path.name + suffix)
        if stale.exists() and not (src / stale.name).exists():
            stale.unlink()
            restored.append(f"removed stale {stale.name}")
    anlz_rel = cfg.anlz_dir.relative_to(db_dir)
    if restore_anlz and (src / anlz_rel).exists():
        tmp = cfg.anlz_dir.with_name(cfg.anlz_dir.name + ".restore-tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src / anlz_rel, tmp)
        old = cfg.anlz_dir.with_name(cfg.anlz_dir.name + ".replaced")
        shutil.rmtree(old, ignore_errors=True)
        if cfg.anlz_dir.exists():
            cfg.anlz_dir.rename(old)
        tmp.rename(cfg.anlz_dir)
        shutil.rmtree(old, ignore_errors=True)
        restored.append(str(anlz_rel) + "/")
    return {"restored_from": str(src), "restored": restored, "safety_backup_of_previous_state": safety["backup_path"]}


# --------------------------------------------------------------------------------------
# Action log
# --------------------------------------------------------------------------------------


def log_action(
    action: str,
    status: str,
    *,
    tracks: Iterable[Any] = (),
    backup_path: str | None = None,
    details: Any = None,
    cfg: Config | None = None,
) -> dict[str, Any]:
    cfg = cfg or get_config()
    entry = {
        "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "status": status,
        "tracks": [str(t) for t in tracks],
        "backup_path": backup_path,
        "details": details,
    }
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.log_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def read_action_log(limit: int = 50, cfg: Config | None = None) -> list[dict[str, Any]]:
    cfg = cfg or get_config()
    if not cfg.log_file.exists():
        return []
    lines = cfg.log_file.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line})
    return out


# --------------------------------------------------------------------------------------
# The guard every write goes through
# --------------------------------------------------------------------------------------


def guarded_write(
    action: str,
    *,
    dry_run: bool,
    plan: dict[str, Any],
    apply: Callable[[], Any],
    tracks: Iterable[Any] = (),
    rollback: Callable[[], None] | None = None,
    include_anlz: bool = True,
) -> dict[str, Any]:
    """Run ``apply`` safely, or only describe it when ``dry_run`` is True."""
    tracks = list(tracks)
    if dry_run:
        log_action(action, "DRY-RUN", tracks=tracks, details=plan)
        return {
            "dry_run": True,
            "action": action,
            "plan": plan,
            "note": "Dry-run: er is niets geschreven. Roep opnieuw aan met dry_run=false om uit te voeren.",
        }

    ensure_rekordbox_closed()
    manifest = create_backup(action, include_anlz=include_anlz)
    backup_path = manifest["backup_path"]
    try:
        # re-check right before writing: Rekordbox may have been started meanwhile
        ensure_rekordbox_closed()
        result = apply()
    except Exception as exc:
        if rollback is not None:
            try:
                rollback()
            except Exception:
                pass
        log_action(action, "FAILED", tracks=tracks, backup_path=backup_path, details={"plan": plan, "error": repr(exc)})
        raise RuntimeError(
            f"{action} mislukt: {exc}. Backup van vóór de actie staat in {backup_path} "
            "(herstel met restore_backup indien nodig)."
        ) from exc
    log_action(action, "OK", tracks=tracks, backup_path=backup_path, details={"plan": plan, "result": result})
    return {"dry_run": False, "action": action, "plan": plan, "result": result, "backup_path": backup_path}
