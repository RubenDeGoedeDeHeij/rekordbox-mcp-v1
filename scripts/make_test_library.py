"""Build a throw-away Rekordbox library for tests and demos.

Uses pyrekordbox's own test data (a real, SQLCipher-encrypted Rekordbox 6
master.db, real ANLZ and MySetting files; MIT licensed, see
tests/fixtures/pyrekordbox_testdata/LICENSE-pyrekordbox) and makes it look like
a local library: audio files that really exist, genres/keys/energy, one ANLZ
set, the MySetting files and a duplicate plus a dead database reference.

Usage:  python scripts/make_test_library.py /tmp/rb-test
Then:   RBMCP_DB_PATH=/tmp/rb-test/rekordbox/master.db python -m rekordbox_mcp
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pyrekordbox_testdata"

# ID -> (artist, title, genre, key, bpm, comment, length_sec)
TRACKS = {
    "178162577": ("Loopmasters", "Demo Track 1", "Tech House", "Am", 128.0, "8A - Energy 6", 172),
    "66382436": ("Loopmasters", "Demo Track 2", "Deep House", "C", 120.0, "8B - Energy 4", 128),
    "24401986": ("FX", "NOISE", "Techno", "Em", 132.0, "9A - Energy 8", 300),
    "22784747": ("FX", "SINEWAVE", "Techno", "Bm", 130.0, "10A - Energy 7", 280),
    "249239133": ("FX", "SIREN", "Tech House", "G", 126.0, "9B - Energy 5", 260),
    "181094952": ("FX", "HORN", "Deep House", "Dm", 122.0, "7A - Energy 3", 240),
}
ANLZ_TRACK = "178162577"


def build(root: Path) -> Path:
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    rb = root / "rekordbox"
    music = root / "Music" / "DJ" / "03 Rekordbox Export"
    rb.mkdir(parents=True)
    music.mkdir(parents=True)
    (root / "Music" / "DJ" / "02 Library").mkdir(parents=True)

    shutil.copy(FIXTURES / "master.db", rb / "master.db")
    shutil.copy(FIXTURES / "masterPlaylists6.xml", rb / "masterPlaylists6.xml")
    for f in (FIXTURES / "mysettings").iterdir():
        shutil.copy(f, rb / f.name)

    from rekordbox_mcp.db import _Database, bpm_to_db, tables

    db = _Database(rb / "master.db", db_dir=rb)
    genres: dict[str, object] = {}
    keys = {k.ScaleName: k for k in db.get_key().all()}
    for cid, (artist, title, genre, key, bpm, comment, length) in TRACKS.items():
        c = db.get_content(ID=cid)
        path = music / f"{artist} - {title}.mp3"
        shutil.copy(FIXTURES / "empty.mp3", path)
        c.FolderPath = str(path)
        c.OrgFolderPath = str(path)
        c.FileNameL = path.name
        c.FileType = 1
        c.BitRate = 320
        c.Title = title
        if c.ArtistID is None or c.ArtistName != artist:
            art = db.get_artist(Name=artist).first() or db.add_artist(artist)
            c.ArtistID = art.ID
        if genre not in genres:
            genres[genre] = db.get_genre(Name=genre).first() or db.add_genre(genre)
        c.GenreID = genres[genre].ID
        if key not in keys:
            k = tables.DjmdKey.create(ID=str(db.generate_unused_id(tables.DjmdKey)), ScaleName=key,
                                      Seq=len(keys) + 1)
            db.add(k)
            db.flush()
            keys[key] = k
        c.KeyID = keys[key].ID
        c.BPM = bpm_to_db(bpm)
        c.Commnt = comment
        c.Length = length
    # commit updates of existing rows first: SQLAlchemy cannot sort a flush that mixes
    # the str IDs of existing rows with the int IDs pyrekordbox gives new rows
    db.commit()

    # ANLZ files for Demo Track 1 at the location Rekordbox expects
    anlz_track = db.get_content(ID=ANLZ_TRACK)
    anlz_dir = rb / "share" / Path(anlz_track.AnalysisDataPath.strip("/\\")).parent
    anlz_dir.mkdir(parents=True)
    for f in (FIXTURES / "anlz").iterdir():
        shutil.copy(f, anlz_dir / f.name)

    # A duplicate of Demo Track 1 (second file, lower bitrate) and a dead reference
    dup_path = music / "Loopmasters - Demo Track 1 (copy).mp3"
    shutil.copy(FIXTURES / "empty.mp3", dup_path)
    dup = db.add_content(dup_path, Title="Demo Track 1", ArtistID=anlz_track.ArtistID,
                         BPM=bpm_to_db(128.0), Length=172, BitRate=192, GenreID=anlz_track.GenreID)
    dead_src = music / "Loopmasters - Demo Track 2 (old).mp3"
    shutil.copy(FIXTURES / "empty.mp3", dead_src)
    dead = db.add_content(dead_src, Title="Demo Track 2", ArtistID=db.get_content(ID="66382436").ArtistID,
                          BPM=bpm_to_db(120.0), Length=128, BitRate=320)
    db.commit()
    dead_src.unlink()  # file is gone (e.g. moved to iCloud) but Rekordbox still references it

    # a playlist that uses the dead reference, to check it is re-pointed to the keeper
    folder = db.create_playlist_folder("Test Folder")
    pl = db.create_playlist("Warmup", parent=folder)
    db.add_to_playlist(pl, dead)
    db.add_to_playlist(pl, dup)
    db.commit()
    db.close()
    return rb / "master.db"


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/rb-test")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    print(build(target))
