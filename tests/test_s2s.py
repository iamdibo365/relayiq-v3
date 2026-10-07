"""VOICE_MODE=s2s: OpenAI realtime front door, Claude orchestrator behind the clinic_agent tool."""

import asyncio
import json
import re
import threading
import time

from fastapi.testclient import TestClient

from relayiq.app import create_app
from relayiq.voice.s2s_session import CLINIC_TOOL, OpenAIRealtimeVoice, front_door_instructions
from test_voice_call import _collect, _media, _start


class FakeRealtime:
    """Mimics the OpenAI Realtime server events for one caller utterance per 25 inbound frames.
    The 'model' always acknowledges and calls clinic_agent, then speaks whatever the tool returns."""

    def __init__(self, utterances):
        self.utterances = list(utterances)
        self.sent: list[dict] = []
        self.q: asyncio.Queue = asyncio.Queue()
        self.n = 0
        self.rid = 0
        self.active = ""
        self.done: set[str] = set()

    async def connect(self):
        pass

    async def close(self):
        pass

    async def events(self):
        while True:
            yield await self.q.get()

    async def _respond(self, say: str, call: dict | None = None):
        self.rid += 1
        rid = f"resp_{self.rid}"
        self.active = rid
        await self.q.put({"type": "response.created", "response": {"id": rid}})
        await self.q.put({"type": "response.output_audio.delta", "response_id": rid, "item_id": f"item_{rid}",
                          "delta": "/////w=="})
        await self.q.put({"type": "response.output_audio_transcript.done", "response_id": rid, "transcript": say})
        output = []
        if call:
            await self.q.put({"type": "response.function_call_arguments.done", "response_id": rid, **call})
            output.append({"type": "function_call", **call})
        self.done.add(rid)
        await self.q.put({"type": "response.done", "response": {"id": rid, "status": "completed",
                                                                "output": output}})

    async def send(self, event: dict):
        self.sent.append(event)
        if event["type"] == "response.create":
            instr = (event.get("response") or {}).get("instructions", "")
            m = re.search(r'"(.*)"', instr)
            if m:
                await self._respond(m.group(1))
            else:
                outs = [e for e in self.sent if e["type"] == "conversation.item.create"]
                await self._respond(outs[-1]["item"]["output"])
        elif event["type"] == "response.cancel" and self.active not in self.done:
            await self.q.put({"type": "response.done", "response": {"id": self.active, "status": "cancelled"}})

    async def send_audio(self, mulaw: bytes):
        self.n += 1
        if self.n % 25 == 0 and self.utterances:
            text = self.utterances.pop(0)
            await self.q.put({"type": "input_audio_buffer.speech_started"})
            await self.q.put({"type": "input_audio_buffer.speech_stopped"})
            await self.q.put({"type": "conversation.item.input_audio_transcription.completed", "transcript": text})
            await self._respond("Sure, one sec.", {"name": "clinic_agent", "call_id": f"call_{self.n}",
                                                  "arguments": json.dumps({"request": text})})


def _s2s_platform(make_platform, settings, script, utterances):
    settings.voice_mode = "s2s"
    fake = FakeRealtime(utterances)
    p = make_platform(script=script, realtime_factory=lambda instructions: fake)
    return p, fake


def _ack_marks(ws, msgs):
    for m in msgs:
        if m["event"] == "mark":
            ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": m["mark"]}))


def test_s2s_routes_caller_turn_through_claude(make_platform, settings):
    p, fake = _s2s_platform(make_platform, settings,
                            [{"text": "Your balance is forty five dollars. Want a payment link?"}],
                            ["What's my balance?"])
    with TestClient(create_app(p)) as client, client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps(_start(p, "CAs2s")))
        greet = _collect(ws, lambda m: m[-1]["event"] == "mark")
        _ack_marks(ws, greet)
        for _ in range(25):
            ws.send_text(json.dumps(_media()))
        msgs = _collect(ws, lambda m: sum(x["event"] == "mark" for x in m) >= 2)  # ack, then answer
        _ack_marks(ws, msgs)
        time.sleep(0.2)
        ws.send_text(json.dumps({"event": "stop"}))
    assert fake.sent[0]["type"] == "response.create" and "Thanks for calling" in fake.sent[0]["response"]["instructions"]
    outputs = [e for e in fake.sent if e["type"] == "conversation.item.create"]
    assert outputs and outputs[0]["item"]["type"] == "function_call_output"
    assert "forty five dollars" in outputs[0]["item"]["output"]
    human = [m.text for m in p._test_model.seen[0] if m.type == "human"]
    assert human == ["What's my balance?"]  # Claude sees the caller's words
    agent = p.db.query("SELECT text, total_ms, llm_first_token_ms FROM turns WHERE call_sid='CAs2s' AND role='agent'")
    assert any("forty five dollars" in r["text"] for r in agent)
    assert agent[-1]["total_ms"] is not None


def test_s2s_watchdog_overrides_realtime_model_on_emergency(make_platform, settings):
    p, fake = _s2s_platform(make_platform, settings, [], ["I have chest pain and can't breathe"])
    with TestClient(create_app(p)) as client, client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps(_start(p, "CAs2sem")))
        greet = _collect(ws, lambda m: m[-1]["event"] == "mark")
        _ack_marks(ws, greet)
        for _ in range(25):
            ws.send_text(json.dumps(_media()))
        got = []

        def reader():  # acknowledge every mark like Twilio does, until the socket closes
            while True:
                try:
                    m = json.loads(ws.receive_text())
                except Exception:  # noqa: BLE001
                    return
                got.append(m["event"])
                if m["event"] == "mark":
                    ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": m["mark"]}))
        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + 6
        while not p.twilio.transfers and time.time() < deadline:
            time.sleep(0.05)
        assert "clear" in got  # whatever the realtime model was saying got cut off
        ws.send_text(json.dumps({"event": "stop"}))
    assert p.twilio.transfers == ["CAs2sem"]
    assert p._test_model.seen == []  # Claude never consulted; the deterministic script ran
    said = [e["response"]["instructions"] for e in fake.sent
            if e["type"] == "response.create" and e.get("response")]
    assert any("9 1 1" in s for s in said)


def test_realtime_session_config_shape(settings):
    cfg = OpenAIRealtimeVoice(settings, front_door_instructions("Test Clinic")).session_config()
    sess = cfg["session"]
    assert cfg["type"] == "session.update" and sess["type"] == "realtime"
    assert sess["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert sess["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    assert sess["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert sess["tools"] == [CLINIC_TOOL]
