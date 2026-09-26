#!/usr/bin/env bash
# Start the Rekordbox MCP server (stdio) with the shared DJ-tools venv.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${RBMCP_VENV:-$DIR/../venv}"
cd "$DIR" && exec "$VENV/bin/python" -m rekordbox_mcp "$@"
