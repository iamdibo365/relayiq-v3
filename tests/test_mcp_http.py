"""Customer-journey MCP server over real streamable HTTP (subprocess), read by the real client."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from relayiq.context.client import JourneyClient

ROOT = Path(__file__).resolve().parents[1]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_journey_over_http(tmp_path):
    port = _free_port()
    env = {**os.environ, "DB_PATH": str(tmp_path / "mcp.db"), "MCP_URL": f"http://127.0.0.1:{port}/mcp",
           "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.Popen([sys.executable, "-m", "relayiq.context.mcp_server"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        client = JourneyClient(f"http://127.0.0.1:{port}/mcp", timeout_s=10)
        text = await client.journey("+15555550102")
        assert "Cigna" in text and "web_chat" in text
    finally:
        proc.terminate()
        proc.wait(5)
