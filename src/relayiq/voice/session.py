"""One live phone call: Twilio Media Stream <-> STT <-> agents <-> TTS, with barge-in.

Latency budget per turn (measured and stored in `turns`):
  caller stops speaking -> server VAD (≈550 ms silence) -> final transcript
  -> first LLM token -> first sentence complete -> first TTS audio frame sent to Twilio
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage

from ..agents.context import CallContext
from ..agents.orchestrator import Orchestrator
from ..db import now_iso
from ..overseer.watchdog import Watchdog
from .audio import FRAME_BYTES, frames
from .chunker import SentenceChunker
from .stt import STTEvent, Transcriber
from .tts import Synthesizer

if TYPE_CHECKING:
    from ..platform import Platform

log = logging.getLogger("relayiq.session")

FILLERS = {
    "verify_insurance": "Let me verify that with your insurance plan, one moment.",
    "find_open_slots": "Let me look at the schedule.",
    "transfer_to_agent": "",
    "end_call": "",
    "escalate_to_human": "",
}
DEFAULT_FILLER = "One moment."


class TwilioIO:
    """Outbound half of the Twilio Media Streams protocol."""

    def __init__(self, ws: Any):
        self.ws = ws
        self.stream_sid = ""

    async def send_json(self, msg: dict) -> None:
        await self.ws.send_text(json.dumps(msg))

    async def media(self, mulaw: bytes) -> None:
        for f in frames(mulaw, FRAME_BYTES):
            await self.send_json({"event": "media", "streamSid": self.stream_sid,
                                  "media": {"payload": base64.b64encode(f).decode()}})

    async def mark(self, name: str) -> None:
        await self.send_json({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    async def clear(self) -> None:
        await self.send_json({"event": "clear", "streamSid": self.stream_sid})


class CallSession:
    def __init__(self, ws: Any, platform: "Platform"):
        self.ws = ws
        self.p = platform
        self.io = TwilioIO(ws)
        self.ctx: CallContext | None = None
        self.orch: Orchestrator | None = None
        self.stt: Transcriber | None = None
        self.tts: Synthesizer = platform.tts
        self.watchdog = Watchdog(platform.settings.latency_slo_ms)
        self.turn_task: asyncio.Task | None = None
        self.pending_marks: dict[str, str] = {}
        self.heard: list[str] = []
        self.mark_seq = 0
        self.last_activity = time.monotonic()
        self.speech_stopped_at: float | None = None
        self.closed = False
        self._tasks: list[asyncio.Task] = []
        self._say_lock = asyncio.Lock()
        self.silence_prompts = 0

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        start = await self._await_start()
        if start is None:
            return
        try:
            await self._setup(start)
            self._tasks.append(asyncio.create_task(self._stt_loop()))
            self._tasks.append(asyncio.create_task(self._silence_loop()))
            await self._greet()
            await self._twilio_loop()
        finally:
            await self._teardown()

    async def _await_start(self) -> dict | None:
        while True:
            raw = await self.ws.receive_text()
            msg = json.loads(raw)
            if msg.get("event") == "start":
                return msg
            if msg.get("event") == "stop":
                return None

    async def _setup(self, msg: dict) -> None:
        st = msg["start"]
        self.io.stream_sid = st.get("streamSid") or msg.get("streamSid", "")
        call_sid = st.get("callSid", "")
        params = st.get("customParameters", {}) or {}
        if not self.p.verify_stream_token(call_sid, params.get("token", "")):
            await self.ws.close(code=4403)
            raise PermissionError("bad stream token")
        caller = params.get("from", "")
        s = self.p.settings
        self.ctx = CallContext(call_sid=call_sid, caller_phone=caller, db=self.p.db, settings=s,
                               services=self.p.services(), on_event=self._on_event)
        self.ctx.caller_id_match = self.p.db.patient_by_phone(caller)
        is_admin = bool(s.admin_phone) and self.p.db.patient_by_phone(caller) is None and \
            "".join(c for c in caller if c.isdigit())[-10:] == "".join(
                c for c in s.admin_phone if c.isdigit())[-10:]
        self.ctx.active_agent = "forge" if is_admin else "front_desk"
        self.p.db.execute("INSERT OR REPLACE INTO calls(call_sid, from_number, started_at, active_agent) "
                          "VALUES (?,?,?,?)", (call_sid, caller, now_iso(), self.ctx.active_agent))
        self.orch = Orchestrator(self.ctx, self.p.gateway, self.p.registry, s, self.p.model_factory)
        self.p.live_calls[call_sid] = {"from": caller[-4:], "agent": self.ctx.active_agent,
                                       "started": now_iso(), "transcript": [], "alerts": []}
        journey_task = asyncio.create_task(self.p.journey.journey(caller))
        self.stt = self.p.transcriber_factory()
        await self.stt.connect()
        self.ctx.journey = await journey_task

    async def _teardown(self) -> None:
        self.closed = True
        for t in self._tasks + ([self.turn_task] if self.turn_task else []):
            t.cancel()
        if self.stt:
            await self.stt.close()
        if self.ctx:
            outcome = "; ".join(self.ctx.case_file[-4:]) or "no actions"
            self.p.db.execute("UPDATE calls SET ended_at=?, outcome=? WHERE call_sid=?",
                              (now_iso(), outcome[:500], self.ctx.call_sid))
            if self.ctx.caller_id_match:
                asyncio.create_task(self.p.journey.log_interaction(
                    self.ctx.caller_phone, "call", f"Voice call with RelayIQ: {outcome}"))
            self.p.live_calls.pop(self.ctx.call_sid, None)

    # ------------------------------------------------------------ inbound
    async def _twilio_loop(self) -> None:
        while not self.closed:
            try:
                raw = await self.ws.receive_text()
            except Exception:  # noqa: BLE001  (socket closed)
                break
            msg = json.loads(raw)
            ev = msg.get("event")
            if ev == "media":
                await self.stt.send_audio(base64.b64decode(msg["media"]["payload"]))
            elif ev == "mark":
                name = msg.get("mark", {}).get("name", "")
                sentence = self.pending_marks.pop(name, None)
                if sentence is not None:
                    self.heard.append(sentence)
                    self.last_activity = time.monotonic()
            elif ev == "stop":
                break

    async def _stt_loop(self) -> None:
        async for ev in self.stt.events():
            await self._on_stt(ev)

    async def _on_stt(self, ev: STTEvent) -> None:
        if ev.kind == "speech_started":
            self.last_activity = time.monotonic()
            self.silence_prompts = 0
            if self._agent_busy():
                await self._barge_in()
        elif ev.kind == "speech_stopped":
            self.speech_stopped_at = time.monotonic()
        elif ev.kind == "final" and ev.text:
            self._transcript("caller", ev.text)
            stt_ms = int((time.monotonic() - self.speech_stopped_at) * 1000) if self.speech_stopped_at else None
            self.p.db.execute("INSERT INTO turns(call_sid, ts, role, agent, text, stt_ms) VALUES (?,?,?,?,?,?)",
                              (self.ctx.call_sid, now_iso(), "caller", "", ev.text, stt_ms))
            if self.turn_task and not self.turn_task.done():
                await self._barge_in()
            self.turn_task = asyncio.create_task(self._turn(ev.text))
        elif ev.kind == "error":
            self.live()["alerts"].append(f"STT: {ev.text[:120]}")

    def _agent_busy(self) -> bool:
        return bool(self.pending_marks) or bool(self.turn_task and not self.turn_task.done())

    async def _barge_in(self) -> None:
        await self.io.clear()
        heard = " ".join(self.heard)
        self.pending_marks.clear()
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
            try:
                await self.turn_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.orch.mark_interrupted(heard)
        self.live()["transcript"].append({"role": "system", "text": "(caller interrupted)"})

    # ------------------------------------------------------------ outbound
    async def _say(self, text: str, timing: dict | None = None) -> None:
        if not text.strip():
            return
        async with self._say_lock:
            first = True
            async for mu in self.tts.synthesize(text):
                if first and timing is not None and "tts_first_audio" not in timing:
                    timing["tts_first_audio"] = time.monotonic()
                first = False
                await self.io.media(mu)
            self.mark_seq += 1
            name = f"s{self.mark_seq}"
            self.pending_marks[name] = text
            await self.io.mark(name)

    async def _greet(self) -> None:
        s = self.p.settings
        if self.ctx.active_agent == "forge":
            text = "Forge here. What agent would you like to build or check on?"
        else:
            text = f"Thanks for calling {s.clinic_name}. This is Relay, an AI assistant. How can I help you today?"
        self._transcript("agent", text)
        self.orch.opening_line = text
        self.heard = []
        await self._say(text)

    async def _turn(self, user_text: str) -> None:
        t0 = self.speech_stopped_at or time.monotonic()
        timing: dict[str, float] = {}
        self.heard = []
        action = self.watchdog.on_user(user_text)
        if action.kind in ("emergency", "self_harm", "escalate"):
            self.live()["alerts"].append(action.reason)
            await self._say(action.say, timing)
            self._transcript("agent", action.say)
            self.orch.history.append(HumanMessage(user_text))
            self.orch.history.append(AIMessage(action.say))
            self.ctx.pending_action = {"type": "transfer", "reason": action.reason}
            await self._finish_turn(t0, timing, action.say)
            return

        chunker = SentenceChunker()
        spoken_any = False
        full: list[str] = []
        async for ev in self.orch.respond(user_text):
            if ev.kind == "text":
                if "llm_first_token" not in timing:
                    timing["llm_first_token"] = time.monotonic()
                full.append(ev.value)
                for sentence in chunker.push(ev.value):
                    spoken_any = True
                    await self._say(sentence, timing)
            elif ev.kind == "break":
                full.append(" ")
                for sentence in chunker.flush():
                    spoken_any = True
                    await self._say(sentence, timing)
            elif ev.kind == "tool":
                filler = FILLERS.get(ev.value, DEFAULT_FILLER)
                if filler and not spoken_any and not chunker.buf.strip():
                    spoken_any = True
                    await self._say(filler, timing)
            elif ev.kind == "agent":
                self.live()["agent"] = ev.value
        for sentence in chunker.flush():
            await self._say(sentence, timing)
        text = "".join(full).strip()
        self._transcript("agent", text)
        await self._finish_turn(t0, timing, text)

    async def _finish_turn(self, t0: float, timing: dict, text: str) -> None:
        def ms(key: str) -> int | None:
            return int((timing[key] - t0) * 1000) if key in timing else None
        total = ms("tts_first_audio")
        if total is not None:
            self.watchdog.on_latency(total)
        self.p.db.execute(
            "INSERT INTO turns(call_sid, ts, role, agent, text, llm_first_token_ms, tts_first_audio_ms, "
            "total_ms) VALUES (?,?,?,?,?,?,?,?)",
            (self.ctx.call_sid, now_iso(), "agent", self.ctx.active_agent, text,
             ms("llm_first_token"), total, total))
        self.live()["last_latency_ms"] = total
        self.live()["alerts"] = list(dict.fromkeys(self.live()["alerts"] + self.watchdog.alerts))[-8:]
        action = self.ctx.pending_action
        if action:
            await self._drain_playback()
            self.ctx.pending_action = None
            if action["type"] == "transfer":
                ok = await self.p.twilio.transfer(self.ctx.call_sid)
                if not ok:
                    await self._say("I'm sorry, no one is available to take the call right now. "
                                    "A staff member will call you back today.")
                    await self._drain_playback()
                    await self.p.twilio.hangup(self.ctx.call_sid)
            elif action["type"] == "hangup":
                await self.p.twilio.hangup(self.ctx.call_sid)

    async def _drain_playback(self, timeout: float = 30) -> None:
        end = time.monotonic() + timeout
        while self.pending_marks and time.monotonic() < end:
            await asyncio.sleep(0.1)

    async def _silence_loop(self) -> None:
        while not self.closed:
            await asyncio.sleep(1)
            if self._agent_busy():
                self.last_activity = time.monotonic()
                continue
            if time.monotonic() - self.last_activity > 12:
                self.silence_prompts += 1
                self.last_activity = time.monotonic()
                if self.silence_prompts == 1:
                    await self._say("Are you still there?")
                else:
                    await self._say("I'll let you go for now. Call us back anytime. Goodbye.")
                    await self._drain_playback()
                    await self.p.twilio.hangup(self.ctx.call_sid)
                    return

    # ------------------------------------------------------------ telemetry
    def live(self) -> dict:
        return self.p.live_calls.setdefault(self.ctx.call_sid, {"transcript": [], "alerts": []})

    def _transcript(self, role: str, text: str) -> None:
        if text:
            self.live()["transcript"].append({"role": role, "text": text,
                                              "agent": self.ctx.active_agent if role == "agent" else ""})
            self.live()["transcript"] = self.live()["transcript"][-40:]

    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "tool":
            act = self.watchdog.on_tool(data["decision"])
            if act.kind == "escalate" and self.ctx.pending_action is None:
                self.ctx.pending_action = {"type": "transfer", "reason": act.reason}
        if self.ctx and self.ctx.call_sid in self.p.live_calls:
            self.live().setdefault("events", []).append({"kind": kind, **data})
            self.live()["events"] = self.live()["events"][-30:]
