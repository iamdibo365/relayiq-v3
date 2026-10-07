"""Streaming text-to-speech with OpenAI (gpt-4o-mini-tts), converted to 8 kHz mu-law."""

from __future__ import annotations

import logging
from typing import AsyncIterator, Protocol

from openai import AsyncOpenAI

from ..config import Settings
from .audio import Downsampler24kTo8k, pcm16_to_mulaw

log = logging.getLogger("relayiq.tts")

VOICE_STYLE = (
    "Warm, calm, professional front-desk voice for a medical clinic. Natural pace, "
    "clear enunciation, friendly but efficient. Read dates, times and dollar amounts naturally."
)


class Synthesizer(Protocol):
    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...


class OpenAITTS:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None):
        self.s = settings
        self.client = client or AsyncOpenAI(api_key=settings.openai_api_key)
        self._cache: dict[str, bytes] = {}

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield mu-law 8 kHz audio as it streams back. Short fixed phrases are cached."""
        cacheable = len(text) <= 60
        if cacheable and text in self._cache:
            yield self._cache[text]
            return
        down = Downsampler24kTo8k()
        collected = bytearray()
        async with self.client.audio.speech.with_streaming_response.create(
            model=self.s.tts_model,
            voice=self.s.tts_voice,
            input=text,
            instructions=VOICE_STYLE,
            response_format="pcm",
        ) as resp:
            async for chunk in resp.iter_bytes(4800):
                mu = pcm16_to_mulaw(down.process(chunk))
                if mu:
                    if cacheable:
                        collected.extend(mu)
                    yield mu
        if cacheable:
            self._cache[text] = bytes(collected)
