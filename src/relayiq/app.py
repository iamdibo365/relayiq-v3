"""FastAPI app: Twilio voice webhook + Media Stream WebSocket + dashboard API."""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
from contextlib import asynccontextmanager
from pathlib import Path
from xml.sax.saxutils import quoteattr

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .config import get_settings
from .overseer.sentinel import load_scenarios, run_gate
from .platform import Platform
from .voice.session import CallSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("relayiq.app")

STATIC = Path(__file__).parent / "dashboard"


def create_app(platform: Platform | None = None) -> FastAPI:
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["p"] = platform or Platform(get_settings())
        yield
        for t in list(holder["p"].background):
            t.cancel()

    app = FastAPI(title="RelayIQ v3", lifespan=lifespan)

    def P() -> Platform:
        return holder["p"]

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    # ------------------------------------------------------------------ Twilio
    @app.post("/twilio/voice")
    async def twilio_voice(request: Request):
        s = P().settings
        if not s.twilio_auth_token:
            raise HTTPException(503, "Twilio is not configured")
        form = dict(await request.form())
        from twilio.request_validator import RequestValidator
        url = s.public_base_url.rstrip("/") + "/twilio/voice"
        if not RequestValidator(s.twilio_auth_token).validate(
                url, form, request.headers.get("X-Twilio-Signature", "")):
            log.warning("rejected webhook with bad signature (check PUBLIC_BASE_URL=%s)", url)
            raise HTTPException(403, "bad signature")
        call_sid, caller = form.get("CallSid", ""), form.get("From", "")
        ws_url = s.public_base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f'<Stream url={quoteattr(ws_url + "/twilio/media")}>'
            f'<Parameter name="from" value={quoteattr(caller)}/>'
            f'<Parameter name="token" value={quoteattr(P().stream_token(call_sid))}/>'
            "</Stream></Connect></Response>")
        return Response(content=twiml, media_type="application/xml")

    @app.websocket("/twilio/media")
    async def twilio_media(ws: WebSocket):
        await ws.accept()
        session = CallSession(ws, P())
        try:
            await session.run()
        except PermissionError:
            log.warning("media stream rejected: bad token")
        except Exception:  # noqa: BLE001
            log.exception("call session crashed")

    # ------------------------------------------------------------------ dashboard
    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        db = P().db
        lat = [r["total_ms"] for r in db.query(
            "SELECT total_ms FROM turns WHERE role='agent' AND total_ms IS NOT NULL ORDER BY id DESC LIMIT 200")]
        p50 = int(statistics.median(lat)) if lat else None
        p95 = int(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)]) if lat else None
        agents = [{"name": s.name, "display_name": s.display_name, "purpose": s.purpose,
                   "tools": s.tools, "source": s.source, "status": s.status}
                  for s in P().registry.builtins.values()]
        for r in db.query("SELECT name, display_name, purpose, tools, status, eval_report FROM agent_specs "
                          "ORDER BY updated_at DESC"):
            rep = json.loads(r["eval_report"]) if r["eval_report"] else None
            agents.append({"name": r["name"], "display_name": r["display_name"], "purpose": r["purpose"],
                           "tools": json.loads(r["tools"]), "source": "forge", "status": r["status"],
                           "eval": {k: rep[k] for k in ("passed", "mean_score", "n", "n_passed")} if rep else None})
        return {
            "live_calls": P().live_calls,
            "metrics": {
                "calls": db.one("SELECT COUNT(*) AS n FROM calls WHERE call_sid NOT LIKE 'sim_%'")["n"],
                "escalated": db.one("SELECT COUNT(*) AS n FROM calls WHERE escalated=1")["n"],
                "latency_p50_ms": p50, "latency_p95_ms": p95, "slo_ms": P().settings.latency_slo_ms,
                "minutes_automated": round(db.one("SELECT COALESCE(SUM(minutes_saved),0) AS m FROM ledger")["m"], 1),
                "actions_executed": db.one("SELECT COUNT(*) AS n FROM ledger WHERE decision='executed'")["n"],
                "actions_denied": db.one("SELECT COUNT(*) AS n FROM ledger WHERE decision='denied'")["n"],
            },
            "recent_calls": db.query("SELECT call_sid, substr(from_number,-4) AS from_last4, started_at, "
                                     "ended_at, active_agent, escalated, outcome FROM calls "
                                     "ORDER BY started_at DESC LIMIT 10"),
            "ledger": db.query("SELECT ts, agent, tool, scope, decision, args, result, minutes_saved, latency_ms "
                               "FROM ledger ORDER BY id DESC LIMIT 25"),
            "insurance_checks": db.query("SELECT id, patient_id, payer_id, method, status, summary, created_at "
                                         "FROM insurance_checks ORDER BY created_at DESC LIMIT 10"),
            "appointments": db.query("SELECT id, patient_id, start, status, insurance_status FROM appointments "
                                     "ORDER BY created_at DESC LIMIT 10"),
            "agents": agents,
            "builds": list(P().forge.jobs.values())[-5:],
            "evals": db.query("SELECT id, target, started_at, completed_at, passed, score FROM eval_runs "
                              "ORDER BY started_at DESC LIMIT 8"),
        }

    class ForgeReq(BaseModel):
        description: str

    @app.post("/api/forge")
    async def forge(req: ForgeReq):
        return P().forge.start(req.description, built_by="dashboard")

    @app.post("/api/evals/run")
    async def run_evals():
        task = asyncio.create_task(run_gate(P(), load_scenarios(), target="platform"))
        P().background.add(task)
        task.add_done_callback(P().background.discard)
        return {"status": "started"}

    @app.get("/api/evals/{run_id}")
    async def eval_report(run_id: str):
        row = P().db.one("SELECT report FROM eval_runs WHERE id=?", (run_id,))
        if not row or not row["report"]:
            raise HTTPException(404, "not ready")
        return JSONResponse(json.loads(row["report"]))

    return app


app = create_app()
