"""MCP client for the customer-journey context server."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("relayiq.mcp")


def _text(result: Any) -> str:
    parts = []
    for block in getattr(result, "content", []) or []:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


class JourneyClient:
    """Thin wrapper so the voice path can fetch/log context with a hard timeout.
    `server` is a URL (streamable HTTP) or, in tests, an in-process MCPServer."""

    def __init__(self, server: Any, timeout_s: float = 2.5):
        self.server = server
        self.timeout_s = timeout_s

    async def call(self, tool: str, args: dict) -> str:
        from mcp import Client

        async def _go() -> str:
            async with Client(self.server) as client:
                res = await client.call_tool(tool, args)
                if getattr(res, "is_error", False) or getattr(res, "isError", False):
                    raise RuntimeError(_text(res))
                return _text(res)

        return await asyncio.wait_for(_go(), self.timeout_s)

    async def journey(self, phone: str) -> str:
        try:
            return await self.call("get_patient_journey", {"phone": phone})
        except Exception as e:  # noqa: BLE001
            log.warning("journey context unavailable: %s", e)
            return ""

    async def log_interaction(self, phone: str, channel: str, summary: str) -> None:
        try:
            await self.call("log_interaction", {"phone": phone, "channel": channel, "summary": summary})
        except Exception as e:  # noqa: BLE001
            log.warning("could not log interaction: %s", e)
