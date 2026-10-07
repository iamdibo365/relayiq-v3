"""Customer-journey context MCP server.

One place that knows what happened with a patient across channels (calls, SMS, portal, web chat,
email, staff tasks). The voice agent reads it at call start; any other agent or product that
speaks MCP can read and write the same journey.

Run:  python -m relayiq.context.mcp_server   (streamable HTTP on :8765/mcp)
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from ..config import get_settings
from ..db import Database, seed


def build_server(db: Database) -> MCPServer:
    server = MCPServer("relayiq-customer-journey",
                       instructions="Cross-channel patient journey for Lakeside Family Medicine.")

    @server.tool()
    def get_patient_journey(phone: str, limit: int = 8) -> str:
        """Summarize a patient's recent cross-channel journey, looked up by phone number."""
        p = db.patient_by_phone(phone)
        if not p:
            return "No patient on file for this phone number."
        events = db.query("SELECT channel, ts, summary FROM journey_events WHERE patient_id=? "
                          "ORDER BY ts DESC LIMIT ?", (p["id"], limit))
        appts = db.query("SELECT start, status, visit_type FROM appointments WHERE patient_id=? "
                         "AND status IN ('booked','pending_insurance') ORDER BY start", (p["id"],))
        bal = db.one("SELECT amount_due FROM balances WHERE patient_id=?", (p["id"],))
        lines = [f"Patient {p['id']} (prefers {p['preferred_language']}); insurer {p['payer_name']}; "
                 f"balance ${bal['amount_due'] if bal else 0:.2f}; "
                 f"{len(appts)} upcoming appointment(s)."]
        lines += [f"- {e['ts'][:10]} [{e['channel']}] {e['summary']}" for e in events]
        return "\n".join(lines)

    @server.tool()
    def log_interaction(phone: str, channel: str, summary: str) -> str:
        """Append an interaction to the patient's journey (e.g. a finished call)."""
        p = db.patient_by_phone(phone)
        if not p:
            return "No patient on file; nothing logged."
        db.add_journey(p["id"], channel, summary[:500])
        return "logged"

    @server.tool()
    def journey_stats() -> str:
        """Counts of journey events by channel (for dashboards)."""
        rows = db.query("SELECT channel, COUNT(*) AS n FROM journey_events GROUP BY channel")
        return json.dumps({r["channel"]: r["n"] for r in rows})

    return server


def main() -> None:
    s = get_settings()
    db = Database(s.db_file)
    seed(db, s.clinic_timezone)
    server = build_server(db)
    from urllib.parse import urlparse
    u = urlparse(s.mcp_url)
    server.run(transport="streamable-http", host=u.hostname or "127.0.0.1", port=u.port or 8765,
               streamable_http_path=u.path or "/mcp")


if __name__ == "__main__":
    main()
