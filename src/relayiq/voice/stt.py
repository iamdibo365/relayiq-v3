"""Streaming speech-to-text over the OpenAI Realtime API (transcription session).

Twilio's 8 kHz mu-law frames are forwarded as-is (format audio/pcmu), so there is no
resampling on the input path. Server VAD gives us turn boundaries and barge-in.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

import websockets

from ..config import Settings

log = logging.getLogger("relayiq.stt")


@dataclass
class STTEvent:
    kind: str  # speech_started | speech_stopped | partial | final | error
    text: str = ""
    item_id: str = ""


class Transcriber(Protocol):
    async def connect(self) -> None: ...
    async def send_audio(self, mulaw: bytes) -> None: ...
    def events(self) -> AsyncIterator[STTEvent]: ...
    async def close(self) -> None: ...


class OpenAIRealtimeTranscriber:
    def __init__(self, settings: Settings, prompt: str = "", language: str = "en"):
        self.s = settings
        self.prompt = prompt
        self.language = language
        self._ws = None
        self._closed = False

    def session_config(self) -> dict:
        transcription: dict = {"model": self.s.stt_model, "language": self.language}
        if self.prompt:
            transcription["prompt"] = self.prompt
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "transcription": transcription,
                        "noise_reduction": {"type": "near_field"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.55,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 550,
                        },
                    }
                },
            },
        }

    async def connect(self) -> None:
        if not self.s.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self._ws = await websockets.connect(
            self.s.openai_realtime_url,
            additional_headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
            max_size=None,
            ping_interval=20,
        )
        await self._ws.send(json.dumps(self.session_config()))

    async def send_audio(self, mulaw: bytes) -> None:
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(mulaw).decode(),
            }))
        except websockets.ConnectionClosed:
            self._closed = True

    async def events(self) -> AsyncIterator[STTEvent]:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                ev = json.loads(raw)
                t = ev.get("type", "")
                if t == "input_audio_buffer.speech_started":
                    yield STTEvent("speech_started", item_id=ev.get("item_id", ""))
                elif t == "input_audio_buffer.speech_stopped":
                    yield STTEvent("speech_stopped", item_id=ev.get("item_id", ""))
                elif t == "conversation.item.input_audio_transcription.delta":
                    yield STTEvent("partial", ev.get("delta", ""), ev.get("item_id", ""))
                elif t == "conversation.item.input_audio_transcription.completed":
                    yield STTEvent("final", (ev.get("transcript") or "").strip(),
                                   ev.get("item_id", ""))
                elif t in ("error", "conversation.item.input_audio_transcription.failed"):
                    err = ev.get("error", ev)
                    log.error("STT error: %s", err)
                    yield STTEvent("error", json.dumps(err)[:500])
                elif t in ("session.created", "session.updated", "transcription_session.updated"):
                    log.info("STT %s", t)
        except websockets.ConnectionClosed as e:
            if not self._closed:
                log.warning("STT socket closed: %s", e)
                yield STTEvent("error", f"stt socket closed: {e}")

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), 2)
            except Exception:  # noqa: BLE001
                pass
