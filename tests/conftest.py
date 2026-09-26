import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A throw-away Rekordbox library (see scripts/make_test_library.py)."""
    from scripts.make_test_library import build

    db_path = build(tmp_path / "lib")
    monkeypatch.setenv("RBMCP_DB_PATH", str(db_path))
    monkeypatch.setenv("RBMCP_HOME", str(tmp_path / "tool"))
    monkeypatch.setenv("RBMCP_LIBRARY_ROOT", str(tmp_path / "lib" / "Music" / "DJ" / "02 Library"))
    monkeypatch.delenv("RBMCP_BACKUP_DIR", raising=False)
    monkeypatch.delenv("RBMCP_LOG_FILE", raising=False)
    return tmp_path
