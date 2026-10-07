#!/usr/bin/env bash
# Run the customer-journey MCP server and the voice app together; Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."
trap 'kill 0' EXIT
uv run python -m relayiq.context.mcp_server &
sleep 2
uv run uvicorn relayiq.app:app --host 0.0.0.0 --port 8000 --app-dir src
