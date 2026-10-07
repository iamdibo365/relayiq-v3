"""VOICE_MODE=s2s: an OpenAI Realtime speech-to-speech model is the "front door"; Claude stays
the brain.

Why: in the chained pipeline the caller waits for transcription -> Claude -> TTS in series.
A realtime model hears audio directly and can answer within a few hundred milliseconds. But we
don't want a second, unaudited brain booking appointments, so the realtime model gets exactly
one tool, `clinic_agent`, which runs the same Claude Orchestrator + ToolGateway as the chained
mode (same agents, allow-lists, confirmations, ledger). The realtime model handles turn-taking,
the quick acknowledgement ("Sure, one sec") and the voice; Claude decides what is true and what
happens.

Twilio <-> OpenAI audio is mu-law 8 kHz on both sides (audio/pcmu), so there is no resampling.
The deterministic watchdog still runs on the input transcription and can override the model.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol

import websockets

from ..config import Settings
from ..db import now_iso
from .session import CallSession

if TYPE_CHECKING:
    from ..platform import Platform

log = logging.getLogger("relayiq.s2s")

CLINIC_TOOL = {
    "type": "function",
    "name": "clinic_agent",
    "description": (
        "The clinic's back-office agent. It knows the patient records, schedule, insurance, refills, "
        "billing and policies, and it is the only way to look anything up or change anything. Pass "
        "the caller's request in their own words, including any names, dates of birth, member IDs or "
        "yes/no confirmations they just gave. Returns what to say to the caller."),
    "parameters": {
        "type": "object",
        "properties": {"request": {"type": "string", "description": "What the caller just said, verbatim."}},
        "required": ["request"],
    },
}


def front_door_instructions(clinic: str) -> str:
    return f"""You are Relay, the phone voice of {clinic}, a medical clinic. You are talking to a caller \
on the phone. Speak English, warmly and briefly, like a good front-desk person.

You do not know anything about the clinic, its patients, schedules, insurance, prices or policies. \
A back-office agent does. For EVERY caller turn that is not pure small talk (hello, thanks, "can you \
repeat that"), call the clinic_agent tool with the caller's words. Before the tool call, say only a \
2-4 word acknowledgement ("Sure, one sec." "Got it." "Let me check."). Never answer a clinic question \
yourself and never guess.

When clinic_agent returns, say its reply to the caller: keep its meaning, facts, names, times and \
numbers exactly; you may smooth the wording for speech. Do not add offers, facts or promises of your \
own. If the reply asks the caller a question, ask it and stop. If the reply says the call is being \
transferred or is ending, say it and stop.

Never read out anything in brackets or ids. Keep every reply under three sentences."""


class RealtimeConnection(Protocol):
    async def connect(self) -> None: ...
    async def send(self, event: dict) -> None: ...
    async def send_audio(self, mulaw: bytes) -> None: ...
    def events(self) -> AsyncIterator[dict]: ...
    async def close(self) -> None: ...


class OpenAIRealtimeVoice:
    """Thin wrapper over the OpenAI Realtime WebSocket (GA interface, session type 'realtime')."""

    def __init__(self, settings: Settings, instructions: str):
        self.s = settings
        self.instructions = instructions
        self._ws = None
        self._closed = False

    def session_config(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.s.realtime_model,
                "instructions": self.instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "transcription": {"model": self.s.stt_model, "language": "en"},
                        "noise_reduction": {"type": "near_field"},
                        "turn_detection": {"type": "server_vad", "threshold": 0.55, "prefix_padding_ms": 300,
                                           "silence_duration_ms": 550, "create_response": True,
                                           "interrupt_response": True},
                    },
                    "output": {"format": {"type": "audio/pcmu"}, "voice": self.s.tts_voice},
                },
                "tools": [CLINIC_TOOL],
                "tool_choice": "auto",
            },
        }

    async def connect(self) -> None:
        if not self.s.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self._ws = await websockets.connect(
            f"{self.s.realtime_url}?model={self.s.realtime_model}",
            additional_headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
            max_size=None, ping_interval=20)
        await self.send(self.session_config())

    async def send(self, event: dict) -> None:
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps(event))
        except websockets.ConnectionClosed:
            self._closed = True

    async def send_audio(self, mulaw: bytes) -> None:
        await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(mulaw).decode()})

    async def events(self) -> AsyncIterator[dict]:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                yield json.loads(raw)
        except websockets.ConnectionClosed as e:
            if not self._closed:
                yield {"type": "error", "error": {"message": f"realtime socket closed: {e}"}}

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), 2)
            except Exception:  # noqa: BLE001
                pass


class S2SCallSession(CallSession):
    """Same lifecycle, Twilio I/O, dashboard and ledger as CallSession; the realtime model replaces
    STT + TTS, and Claude is reached through the clinic_agent tool."""

    def __init__(self, ws: Any, platform: "Platform"):
        super().__init__(ws, platform)
        self.rt: RealtimeConnection | None = None
        self.responding = False          # a realtime response is in progress
        self.audio_item = ""             # assistant item currently being played
        self.audio_started_at = 0.0
        self.audio_ms_sent = 0
        self.timing: dict[str, float] = {}
        self.agent_text: list[str] = []
        self.tool_tasks: set[asyncio.Task] = set()
        self.override = False            # watchdog took over this turn
        self.response_id = ""
        self.cancelled_response = ""
        self._queued: list[dict] = []    # response.create waiting for the active response to finish
        self.override_say = ""           # watchdog script waiting for the model's own response to be cancelled

    # ------------------------------------------------------------ wiring
    async def _connect_audio(self) -> None:
        factory = getattr(self.p, "realtime_factory", None)
        instructions = front_door_instructions(self.p.settings.clinic_name)
        self.rt = factory(instructions) if factory else OpenAIRealtimeVoice(self.p.settings, instructions)
        await self.rt.connect()
        self.stt = self.rt  # CallSession._twilio_loop forwards inbound audio via self.stt.send_audio

    async def _stt_loop(self) -> None:
        async for ev in self.rt.events():
            try:
                await self._on_rt(ev)
            except Exception:  # noqa: BLE001
                log.exception("realtime event handling failed: %s", ev.get("type"))

    def _agent_busy(self) -> bool:
        return self.responding or bool(self.pending_marks) or any(not t.done() for t in self.tool_tasks)

    async def _say(self, text: str, timing: dict | None = None) -> None:
        """Make the realtime voice say a fixed line (greeting, watchdog script, silence prompts)."""
        if not text.strip():
            return
        await self._create({"type": "response.create", "response": {
            "instructions": f"Say exactly this, word for word, and nothing else: \"{text}\""}})

    async def _create(self, event: dict) -> None:
        """Only one response may be active at a time; queue until the current one is done."""
        if self.responding:
            self._queued.append(event)
        else:
            self.responding = True  # optimistic until response.created/done arrives
            await self.rt.send(event)

    async def _greet(self) -> None:
        text = (f"Thanks for calling {self.p.settings.clinic_name}. This is Relay, an AI assistant. "
                "How can I help you today?")
        self.orch.opening_line = text
        self._transcript("agent", text)
        await self._say(text)

    # ------------------------------------------------------------ realtime events
    async def _on_rt(self, ev: dict) -> None:
        t = ev.get("type", "")
        if t == "input_audio_buffer.speech_started":
            self.last_activity = time.monotonic()
            self.silence_prompts = 0
            if self.audio_item or self.pending_marks:
                await self._barge_in_rt()
        elif t == "input_audio_buffer.speech_stopped":
            self.speech_stopped_at = time.monotonic()
            self.timing, self.agent_text, self.override = {}, [], False
        elif t == "conversation.item.input_audio_transcription.completed":
            await self._on_caller_text((ev.get("transcript") or "").strip())
        elif t == "response.created":
            self.responding = True
            self.response_id = ev.get("response", {}).get("id", "")
            if self.override_say:
                # The model's automatic reply to an emergency utterance: cancel it, the script goes next.
                self.cancelled_response = self.response_id
                await self.rt.send({"type": "response.cancel"})
        elif t == "response.output_audio.delta":
            if ev.get("response_id") and ev.get("response_id") == self.cancelled_response:
                return
            mu = base64.b64decode(ev.get("delta", ""))
            if ev.get("item_id") != self.audio_item:
                self.audio_item, self.audio_started_at, self.audio_ms_sent = ev.get("item_id", ""), \
                    time.monotonic(), 0
            if "tts_first_audio" not in self.timing and self.speech_stopped_at:
                self.timing["tts_first_audio"] = time.monotonic()
            self.audio_ms_sent += len(mu) // 8
            await self.io.media(mu)
        elif t == "response.output_audio_transcript.done":
            if ev.get("transcript"):
                self.agent_text.append(ev["transcript"])
        elif t == "response.function_call_arguments.done":
            if ev.get("name") == "clinic_agent" and not self.override:
                task = asyncio.create_task(self._run_clinic_agent(ev.get("call_id", ""), ev.get("arguments", "{}")))
                self.tool_tasks.add(task)
                task.add_done_callback(self.tool_tasks.discard)
        elif t == "response.done":
            self.responding = False
            response = ev.get("response", {})
            if not (response.get("id") and response.get("id") == self.cancelled_response):
                await self._on_response_done(response)
            if self.override_say:
                await self._say_override()
            elif self._queued:
                await self._create(self._queued.pop(0))
        elif t == "error":
            err = ev.get("error", {})
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if "no active response" not in msg.lower():
                log.error("realtime error: %s", msg)
                self.live()["alerts"].append(f"Realtime: {msg[:120]}")

    async def _on_caller_text(self, text: str) -> None:
        if not text:
            return
        self._transcript("caller", text)
        stt_ms = int((time.monotonic() - self.speech_stopped_at) * 1000) if self.speech_stopped_at else None
        self.p.db.execute("INSERT INTO turns(call_sid, ts, role, agent, text, stt_ms) VALUES (?,?,?,?,?,?)",
                          (self.ctx.call_sid, now_iso(), "caller", "", text, stt_ms))
        action = self.watchdog.on_user(text)
        if action.kind in ("emergency", "self_harm", "escalate"):
            # Deterministic override: whatever the realtime model started saying is cut off.
            self.override = True
            self.live()["alerts"].append(action.reason)
            for task in list(self.tool_tasks):
                task.cancel()
            self._queued.clear()
            await self.io.clear()
            self.pending_marks.clear()
            self.audio_item = ""
            self.ctx.pending_action = {"type": "transfer", "reason": action.reason}
            self._transcript("agent", action.say)
            self.override_say = action.say
            if self.responding:
                self.cancelled_response = self.response_id
                await self.rt.send({"type": "response.cancel"})  # its response.done sends the script
            else:
                # The model's automatic response may still be on its way; give it a moment to show
                # up (response.created cancels it), then speak the script regardless.
                self._tasks.append(asyncio.create_task(self._say_override(delay=0.4)))

    async def _say_override(self, delay: float = 0.0) -> None:
        if delay:
            await asyncio.sleep(delay)
            if self.responding:
                return  # response.done of the cancelled response will call us again
        text, self.override_say = self.override_say, ""
        if text:
            await self._create({"type": "response.create", "response": {
                "instructions": f"Say exactly this, word for word, and nothing else: \"{text}\""}})

    async def _run_clinic_agent(self, call_id: str, arguments: str) -> None:
        try:
            request = json.loads(arguments or "{}").get("request", "")
        except json.JSONDecodeError:
            request = arguments
        started = time.monotonic()
        parts: list[str] = []
        try:
            async for ev in self.orch.respond(request or "(caller said nothing clear)"):
                if ev.kind == "text":
                    if "llm_first_token" not in self.timing:
                        self.timing["llm_first_token"] = time.monotonic()
                    parts.append(ev.value)
                elif ev.kind == "break":
                    parts.append(" ")
                elif ev.kind == "agent":
                    self.live()["agent"] = ev.value
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("clinic agent failed")
            parts = [f"Sorry, I couldn't complete that right now ({type(e).__name__}). "
                     "Let me have a staff member call you back."]
        reply = "".join(parts).strip() or "(no spoken reply)"
        self.timing["claude_done"] = time.monotonic()
        log.info("clinic_agent answered in %d ms", int((time.monotonic() - started) * 1000))
        if self.override:
            return
        await self.rt.send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": reply}})
        await self._create({"type": "response.create"})

    async def _on_response_done(self, response: dict) -> None:
        if self.audio_item:
            self.mark_seq += 1
            name = f"s{self.mark_seq}"
            self.pending_marks[name] = " ".join(self.agent_text)
            await self.io.mark(name)
            self.audio_item = ""
        outputs = response.get("output", []) or []
        called_tool = any(o.get("type") == "function_call" for o in outputs)
        if called_tool or any(not t.done() for t in self.tool_tasks):
            return  # the real answer comes in the next response
        text = " ".join(self.agent_text).strip()
        if self.speech_stopped_at and (text or self.timing):
            if text and not self.override:
                self._transcript("agent", text)
            await self._finish_turn(self.speech_stopped_at, self.timing, text)
            self.speech_stopped_at = None
        elif self.ctx.pending_action:
            await self._finish_turn(time.monotonic(), {}, text)

    async def _barge_in_rt(self) -> None:
        """Caller talked over the agent: stop Twilio playback and tell the model how much was heard."""
        await self.io.clear()
        self.pending_marks.clear()
        if self.audio_item:
            played = int((time.monotonic() - self.audio_started_at) * 1000)
            await self.rt.send({"type": "conversation.item.truncate", "item_id": self.audio_item,
                                "content_index": 0, "audio_end_ms": max(0, min(played, self.audio_ms_sent))})
        self.audio_item = ""
        self.live()["transcript"].append({"role": "system", "text": "(caller interrupted)"})

    async def _teardown(self) -> None:
        for task in list(self.tool_tasks):
            task.cancel()
        await super()._teardown()
