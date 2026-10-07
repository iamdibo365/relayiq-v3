"""Telephony audio helpers: G.711 mu-law <-> PCM16 and 24 kHz -> 8 kHz resampling.

Twilio Media Streams carry 8 kHz mono mu-law in 20 ms frames (160 bytes).
OpenAI TTS 'pcm' output is 24 kHz mono signed 16-bit little-endian.
Pure numpy so it works on Python 3.13+ (audioop was removed).
"""

from __future__ import annotations

import numpy as np

FRAME_BYTES = 160  # 20 ms @ 8 kHz mu-law
_BIAS = 0x84


_SEG_UEND = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])


def pcm16_to_mulaw(pcm: np.ndarray) -> bytes:
    """Encode int16 samples to G.711 mu-law bytes (bit-exact with the classic Sun g711.c)."""
    x = pcm.astype(np.int32) >> 2  # 16-bit -> 14-bit
    mask = np.where(x < 0, 0x7F, 0xFF)
    x = np.minimum(np.abs(x), 8159) + (_BIAS >> 2)
    seg = np.searchsorted(_SEG_UEND, x)  # first seg with x <= uend
    uval = (np.minimum(seg, 7) << 4) | ((x >> (np.minimum(seg, 7) + 1)) & 0x0F)
    uval = np.where(seg >= 8, 0x7F, uval)
    return ((uval ^ mask) & 0xFF).astype(np.uint8).tobytes()


def mulaw_to_pcm16(data: bytes) -> np.ndarray:
    """Decode G.711 mu-law bytes to int16 samples."""
    mu = ~np.frombuffer(data, dtype=np.uint8).astype(np.int32) & 0xFF
    sign = mu & 0x80
    exponent = (mu >> 4) & 0x07
    mantissa = mu & 0x0F
    x = ((mantissa << 3) + _BIAS) << exponent
    x = x - _BIAS
    return np.where(sign != 0, -x, x).astype(np.int16)


def _lowpass_taps(num_taps: int = 63, cutoff_hz: float = 3600.0, rate: int = 24000) -> np.ndarray:
    n = np.arange(num_taps) - (num_taps - 1) / 2
    fc = cutoff_hz / rate
    taps = 2 * fc * np.sinc(2 * fc * n) * np.hamming(num_taps)
    return (taps / taps.sum()).astype(np.float64)


class Downsampler24kTo8k:
    """Streaming 3:1 decimator with an anti-alias FIR; keeps filter state between chunks."""

    def __init__(self) -> None:
        self._taps = _lowpass_taps()
        self._hist = np.zeros(len(self._taps) - 1, dtype=np.float64)
        self._phase = 0
        self._carry = b""

    def process(self, pcm24_bytes: bytes) -> np.ndarray:
        data = self._carry + pcm24_bytes
        usable = len(data) - (len(data) % 2)
        self._carry = data[usable:]
        if usable == 0:
            return np.zeros(0, dtype=np.int16)
        x = np.frombuffer(data[:usable], dtype="<i2").astype(np.float64)
        buf = np.concatenate([self._hist, x])
        y = np.convolve(buf, self._taps, mode="valid")
        self._hist = buf[-(len(self._taps) - 1):]
        out = y[self._phase::3]
        self._phase = (self._phase - len(y)) % 3
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def upsample_8k_to_24k(pcm8: np.ndarray) -> np.ndarray:
    """Linear-interpolation upsampler (used by the smoke-test caller and tests)."""
    if len(pcm8) == 0:
        return pcm8
    xp = np.arange(len(pcm8))
    x = np.arange(len(pcm8) * 3) / 3
    return np.interp(x, xp, pcm8.astype(np.float64)).astype(np.int16)


def frames(mulaw: bytes, frame_bytes: int = FRAME_BYTES) -> list[bytes]:
    return [mulaw[i:i + frame_bytes] for i in range(0, len(mulaw), frame_bytes)]


def silence_mulaw(ms: int) -> bytes:
    return b"\xff" * (8 * ms)
