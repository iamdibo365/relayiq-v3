"""Real end-to-end call WITHOUT a phone: acts exactly like Twilio against your running server.

  1. POSTs a correctly signed /twilio/voice webhook and reads the TwiML (stream URL + token)
  2. Opens the Media Stream WebSocket and sends start / media / mark / stop like Twilio does
  3. Caller lines are spoken with real OpenAI TTS and streamed in real time as 8 kHz mu-law
  4. Agent audio comes back from the real stack (OpenAI STT -> Claude agents -> OpenAI TTS)
     and is saved to data/smoke_call.wav; per-turn latency is printed

Usage:  uv run python scripts/smoke_call.py [--base http://localhost:8000] [--caller +15555550101]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
import time
import uuid
import wave
from pathlib import Path

import httpx
import numpy as np
import websockets
from openai import AsyncOpenAI
from twilio.request_validator import RequestValidator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from relayiq.config import get_settings  # noqa: E402
from relayiq.voice.audio import (  # noqa: E402
    Downsampler24kTo8k,
    frames,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    silence_mulaw,
)

DEFAULT_LINES = [
    "Hi, I'd like to book a follow-up appointment with Dr. Chen.",
    "John Doe, April twelfth, nineteen eighty.",
    "Next week in the morning works.",
    "The first one is perfect.",
    "Yes, that's correct, please book it.",
    "No, that's all. Thank you!",
]


async def tts_mulaw(client: AsyncOpenAI, text: str, voice: str) -> bytes:
    down = Downsampler24kTo8k()
    out = bytearray()
    async with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts", voice=voice, input=text, response_format="pcm") as resp:
        async for chunk in resp.iter_bytes(4800):
            out += pcm16_to_mulaw(down.process(chunk))
    return bytes(out)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--caller", default="+15555550101")
    ap.add_argument("--lines", nargs="*", default=DEFAULT_LINES)
    args = ap.parse_args()
    s = get_settings()
    if not (s.openai_api_key and s.anthropic_api_key and s.twilio_auth_token):
        sys.exit("Need OPENAI_API_KEY, ANTHROPIC_API_KEY and TWILIO_AUTH_TOKEN in .env")

    call_sid = "CAsmoke" + uuid.uuid4().hex[:24]
    params = {"CallSid": call_sid, "From": args.caller, "To": s.twilio_phone_number or "+15550000000",
              "AccountSid": s.twilio_account_sid or "AC0"}
    signed_url = s.public_base_url.rstrip("/") + "/twilio/voice"
    sig = RequestValidator(s.twilio_auth_token).compute_signature(signed_url, params)
    async with httpx.AsyncClient() as http:
        r = await http.post(args.base.rstrip("/") + "/twilio/voice", data=params,
                            headers={"X-Twilio-Signature": sig})
    r.raise_for_status()
    token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
    ws_url = args.base.replace("http", "ws", 1).rstrip("/") + "/twilio/media"
    print(f"webhook OK -> streaming to {ws_url}")

    oa = AsyncOpenAI(api_key=s.openai_api_key)
    print("synthesizing caller lines ...")
    caller_audio = [await tts_mulaw(oa, line, "echo") for line in args.lines]

    received = bytearray()
    playback_until = 0.0
    last_agent_audio = time.monotonic()
    first_audio_after: dict[int, float] = {}
    turn_idx = -1
    turn_ended_at = 0.0

    async with websockets.connect(ws_url, max_size=None) as ws:
        async def send(msg):
            await ws.send(json.dumps(msg))

        await send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        await send({"event": "start", "streamSid": "MZsmoke", "start": {
            "streamSid": "MZsmoke", "callSid": call_sid, "accountSid": params["AccountSid"],
            "tracks": ["inbound"], "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {"from": args.caller, "token": token}}})

        async def reader():
            nonlocal playback_until, last_agent_audio
            async for raw in ws:
                msg = json.loads(raw)
                ev = msg.get("event")
                now = time.monotonic()
                if ev == "media":
                    chunk = base64.b64decode(msg["media"]["payload"])
                    received.extend(chunk)
                    if turn_idx >= 0 and turn_idx not in first_audio_after:
                        first_audio_after[turn_idx] = now - turn_ended_at
                    playback_until = max(playback_until, now) + len(chunk) / 8000
                    last_agent_audio = now
                elif ev == "mark":
                    delay = max(0.0, playback_until - now)  # Twilio echoes a mark once audio has played
                    asyncio.get_running_loop().call_later(delay, lambda m=msg: asyncio.ensure_future(
                        send({"event": "mark", "streamSid": "MZsmoke", "mark": m["mark"]})))
                elif ev == "clear":
                    print("   (agent audio cleared - barge-in)")
                    playback_until = now

        rtask = asyncio.create_task(reader())

        async def stream(audio: bytes):
            for f in frames(audio):
                await send({"event": "media", "streamSid": "MZsmoke", "media": {
                    "track": "inbound", "payload": base64.b64encode(f).decode()}})
                await asyncio.sleep(0.02)

        async def wait_agent_done(turn: int | None = None, timeout=30.0):
            """Like a real caller: wait for the agent to START answering, then for it to finish.
            (Silence while the agent is thinking is not the end of its turn.)"""
            start = time.monotonic()
            while time.monotonic() - start < timeout:  # 1) wait for the reply to begin
                started = (turn in first_audio_after) if turn is not None else len(received) > 0
                if started:
                    break
                await stream(silence_mulaw(100))
            while time.monotonic() - start < timeout:  # 2) wait for it to finish playing
                idle = time.monotonic() - last_agent_audio
                if idle > 1.8 and time.monotonic() > playback_until + 0.5:
                    return
                await stream(silence_mulaw(100))

        await wait_agent_done()
        print("agent greeted")
        for i, (line, audio) in enumerate(zip(args.lines, caller_audio)):
            print(f"\nCALLER: {line}")
            await stream(audio)
            turn_idx, turn_ended_at = i, time.monotonic()
            await wait_agent_done(turn=i)
            lat = first_audio_after.get(i)
            print(f"   agent replied; first audio {lat*1000:.0f} ms after caller stopped" if lat
                  else "   (no agent audio this turn)")
        await send({"event": "stop", "streamSid": "MZsmoke"})
        await asyncio.sleep(0.5)
        rtask.cancel()

    out = Path("data/smoke_call.wav")
    out.parent.mkdir(exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(mulaw_to_pcm16(bytes(received)).astype("<i2").tobytes())
    lats = [v * 1000 for v in first_audio_after.values()]
    print(f"\nSaved agent audio to {out} ({len(received)/8000:.1f}s)")
    if lats:
        print(f"Turn latency (caller stops -> first agent audio): median {np.median(lats):.0f} ms, "
              f"max {max(lats):.0f} ms. Transcript + tool calls: open the dashboard.")


if __name__ == "__main__":
    asyncio.run(main())
