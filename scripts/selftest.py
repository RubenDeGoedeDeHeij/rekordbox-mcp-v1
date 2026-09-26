"""End-to-end self-test over real MCP stdio.

Starts the server as a subprocess (exactly like Claude Desktop does) and calls
the read tools plus DRY-RUN write tools, and backup_now. Nothing in your
Rekordbox library is modified by this script except that a backup folder is
created (which is verified with `ls -la`).

    # against your real library (Rekordbox may be open; only reads + backup)
    ~/Music/DJ/05\\ Tools/venv/bin/python scripts/selftest.py

    # against a throw-away test library
    python scripts/selftest.py --test-library /tmp/rb-test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


def _text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


def _show(title: str, text: str, max_lines: int = 60) -> dict | list:
    print(f"\n=== {title} ===")
    lines = text.splitlines()
    print("\n".join(lines[:max_lines]) + (f"\n... ({len(lines) - max_lines} regels meer)" if len(lines) > max_lines else ""))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


async def run(env: dict[str, str], cue_track: str | None) -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "rekordbox_mcp"], env=env, cwd=str(ROOT))
    failures = 0
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"Server gestart; {len(names)} tools: {', '.join(names)}")

            async def call(name: str, args: dict | None = None, max_lines: int = 60):
                nonlocal failures
                res = await session.call_tool(name, args or {})
                data = _show(f"{name}({json.dumps(args or {}, ensure_ascii=False)})", _text(res), max_lines)
                if isinstance(data, dict) and "error" in data:
                    failures += 1
                return data

            await call("get_status")
            pls = await call("list_playlists", max_lines=40)
            await call("create_playlist", {"name": "MCP Selftest", "parent": "Sets/Selftest", "dry_run": True})

            backup = await call("backup_now", {"label": "selftest"})
            if isinstance(backup, dict) and backup.get("backup_path"):
                print("\n=== ls -la van de backupmap ===")
                print(subprocess.run(["ls", "-la", backup["backup_path"]], capture_output=True, text=True).stdout)
                if not Path(backup["backup_path"], Path(env.get("RBMCP_DB_PATH", "master.db")).name).exists():
                    print("!! master.db ontbreekt in de backup")
                    failures += 1
            else:
                print("!! backup_now leverde geen backup op — stop, geen verdere tests")
                return 1

            await call("get_settings", max_lines=40)

            if cue_track is None:
                status = await session.call_tool("search_tracks", {"only_existing_files": True, "limit": 1})
                found = json.loads(_text(status)).get("tracks", [])
                cue_track = found[0]["id"] if found else None
            if cue_track:
                await call("inspect_track_cues", {"track": cue_track})
                await call("write_cue_points", {
                    "track": cue_track,
                    "cues": [{"type": "hot", "slot": "A", "time": "0:01.000", "color": "green", "comment": "selftest"},
                             {"type": "memory", "time": 2.5}],
                    "mode": "dry_run",
                })
            await call("build_set_from_criteria", {"name": "Selftest set", "bpm_min": 60, "bpm_max": 200,
                                                   "max_tracks": 8, "dry_run": True}, max_lines=40)
            await call("dedupe_library", {"action": "report"}, max_lines=40)
            await call("get_action_log", {"limit": 5}, max_lines=30)
    print(f"\nKlaar: {failures} tool(s) gaven een fout." if failures else "\nKlaar: alle aanroepen OK.")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-library", help="bouw eerst een wegwerp-testbibliotheek in deze map en test daartegen")
    ap.add_argument("--db", help="pad naar master.db (standaard: RBMCP_DB_PATH of ~/Library/Pioneer/rekordbox/master.db)")
    ap.add_argument("--cue-track", help="track (ID of 'Artiest - Titel') voor de cue dry-run")
    args = ap.parse_args()

    env = dict(os.environ)
    if args.test_library:
        sys.path.insert(0, str(ROOT))
        from scripts.make_test_library import build

        db_path = build(Path(args.test_library))
        env["RBMCP_DB_PATH"] = str(db_path)
        env.setdefault("RBMCP_HOME", str(Path(args.test_library) / "tool"))
        env.setdefault("RBMCP_LIBRARY_ROOT", str(Path(args.test_library) / "Music" / "DJ" / "02 Library"))
    elif args.db:
        env["RBMCP_DB_PATH"] = args.db
    return asyncio.run(run(env, args.cue_track))


if __name__ == "__main__":
    raise SystemExit(main())
