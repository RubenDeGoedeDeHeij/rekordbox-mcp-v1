"""Paths and settings for the Rekordbox MCP server.

Everything can be overridden with environment variables so the same code works
against the real library (default) and against a test copy of the database.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

# Tool directory = the folder that contains this package (e.g.
# ~/Music/DJ/05 Tools/rekordbox-mcp). Backups and logs live here by default.
TOOL_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path | None) -> Path | None:
    value = os.environ.get(name)
    if value:
        return Path(value).expanduser()
    return default


def _default_db_path() -> Path | None:
    """Default location of master.db for Rekordbox 6/7."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Pioneer" / "rekordbox" / "master.db"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Pioneer" / "rekordbox" / "master.db"
    return None


@dataclass
class Config:
    db_path: Path | None = field(default_factory=lambda: _env_path("RBMCP_DB_PATH", _default_db_path()))
    db_key: str = field(default_factory=lambda: os.environ.get("RBMCP_DB_KEY", ""))
    tool_dir: Path = field(default_factory=lambda: _env_path("RBMCP_HOME", TOOL_DIR))
    library_root: Path = field(
        default_factory=lambda: _env_path("RBMCP_LIBRARY_ROOT", Path.home() / "Music" / "DJ" / "02 Library")
    )
    # 0 = keep every backup (default, safest). N > 0 = keep the N newest.
    backup_keep: int = field(default_factory=lambda: int(os.environ.get("RBMCP_BACKUP_KEEP", "0")))

    @property
    def db_dir(self) -> Path:
        if self.db_path is None:
            raise RuntimeError(
                "Rekordbox database not found. Set RBMCP_DB_PATH to the full path of master.db "
                "(macOS default: ~/Library/Pioneer/rekordbox/master.db)."
            )
        return self.db_path.parent

    @property
    def anlz_dir(self) -> Path:
        """Root of the ANLZ analysis files (cues, beatgrids, waveforms)."""
        return self.db_dir / "share" / "PIONEER" / "USBANLZ"

    @property
    def backup_dir(self) -> Path:
        return _env_path("RBMCP_BACKUP_DIR", self.tool_dir / "backups")

    @property
    def log_file(self) -> Path:
        return _env_path("RBMCP_LOG_FILE", self.tool_dir / "logs" / "write-actions.log")

    @property
    def trash_dir(self) -> Path:
        return self.tool_dir / "duplicates_trash"


def get_config() -> Config:
    # Re-read the environment on every call so tests can swap databases.
    return Config()
