"""Test doubles (tests only - the shipped app uses real Anthropic/OpenAI/Twilio/Stedi)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Callable

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from relayiq.config import Settings
from relayiq.context.client import JourneyClient
from relayiq.context.mcp_server import build_server
from relayiq.db import Database
from relayiq.platform import Platform
from relayiq.voice.stt import STTEvent


class ScriptedModel(BaseChatModel):
    """Returns scripted turns in order. Each turn: {"text": str, "tools": [(name, args), ...]}
    or a callable(messages) -> turn."""

    script: list = []
    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _next(self, messages) -> AIMessage:
        self.seen.append(messages)
        turn = self.script.pop(0) if self.script else {"text": "Okay."}
        if callable(turn):
            turn = turn(messages)
        calls = [{"name": n, "args": a, "id": "call_" + uuid.uuid4().hex[:8], "type": "tool_call"}
                 for n, a in turn.get("tools", [])]
        return AIMessage(content=turn.get("text", ""), tool_calls=calls, id="msg_" + uuid.uuid4().hex[:8])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self._next(messages)
        words = msg.content.split(" ") if msg.content else []
        if not words and not msg.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", id=msg.id))
            return
        for i, w in enumerate(words):
            chunk = ChatGenerationChunk(message=AIMessageChunk(
                content=w + (" " if i < len(words) - 1 else ""), id=msg.id))
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk
        for idx, tc in enumerate(msg.tool_calls):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content="", id=msg.id, tool_call_chunks=[
                {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": idx}]))
            if run_manager:
                run_manager.on_llm_new_token("", chunk=chunk)
            yield chunk


class FakeTranscriber:
    """Emits one scripted utterance each time `frames_per_utterance` inbound frames arrive."""

    def __init__(self, utterances: list[str], frames_per_utterance: int = 25):
        self.utterances = list(utterances)
        self.n = 0
        self.per = frames_per_utterance
        self.q: asyncio.Queue = asyncio.Queue()
        self.connected = False

    async def connect(self):
        self.connected = True

    async def send_audio(self, mulaw: bytes):
        self.n += 1
        if self.n % self.per == 0 and self.utterances:
            u = self.utterances.pop(0)
            if isinstance(u, dict):  # {"partial": str, "final": str, "delay": s}: streaming transcript
                for ev in (STTEvent("speech_started"), STTEvent("speech_stopped"),
                           STTEvent("partial", u["partial"])):
                    await self.q.put(ev)

                async def later():
                    await asyncio.sleep(u.get("delay", 0.3))
                    await self.q.put(STTEvent("final", u["final"]))
                self._later = asyncio.create_task(later())
                return
            for ev in (STTEvent("speech_started"), STTEvent("speech_stopped"), STTEvent("final", u)):
                await self.q.put(ev)

    async def events(self):
        while True:
            yield await self.q.get()

    async def close(self):
        pass


class FakeTTS:
    def __init__(self):
        self.spoken: list[str] = []

    async def synthesize(self, text: str):
        self.spoken.append(text)
        yield b"\xff" * 320  # 40 ms of silence per sentence


class FakeTwilio:
    def __init__(self):
        self.sms: list[tuple[str, str]] = []
        self.transfers: list[str] = []
        self.hangups: list[str] = []

    async def send(self, to, body):
        self.sms.append((to, body))

    async def transfer(self, call_sid, say=""):
        self.transfers.append(call_sid)
        return True

    async def hangup(self, call_sid):
        self.hangups.append(call_sid)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, db_path=str(tmp_path / "t.db"), twilio_auth_token="test-token",
                    public_base_url="https://relay.example.com", anthropic_api_key="x",
                    openai_api_key="x")


@pytest.fixture
def make_platform(settings) -> Callable[..., Platform]:
    def _make(script: list | None = None, utterances: list[str] | None = None, **kw) -> Platform:
        db = Database(settings.db_file)
        model = ScriptedModel(script=script or [])
        stt = FakeTranscriber(utterances or [])
        p = Platform(settings, db=db, model_factory=lambda name, s: model, tts=FakeTTS(),
                     transcriber_factory=lambda: stt, journey=JourneyClient(build_server(db)),
                     twilio=FakeTwilio(), **kw)
        p._test_model, p._test_stt = model, stt
        return p
    return _make
