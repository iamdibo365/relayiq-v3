"""Split streaming LLM text into speakable sentences so TTS can start early."""

from __future__ import annotations

import re

_BOUNDARY = re.compile(r"[.!?](?=\s)")
_ABBREV = ("dr.", "mr.", "mrs.", "ms.", "st.", "ave.", "a.m.", "p.m.", "no.", "e.g.", "i.e.", "vs.")


class SentenceChunker:
    """Emit complete sentences; the first one must be >= first_min_chars so we don't send a
    lone "Sure." to TTS (one extra round trip) when the next sentence is milliseconds away."""

    def __init__(self, max_chars: int = 160, first_min_chars: int = 15):
        self.buf = ""
        self.max_chars = max_chars
        self.first_min_chars = first_min_chars
        self.emitted = 0

    def _next_cut(self) -> int | None:
        min_len = self.first_min_chars if self.emitted == 0 else 2
        for m in _BOUNDARY.finditer(self.buf):
            end = m.end()
            head = self.buf[:end].strip()
            if head.lower().endswith(_ABBREV):
                continue
            if len(head) >= min_len:
                return end
        if len(self.buf) > self.max_chars:
            cut = max(self.buf.rfind(",", 0, self.max_chars), self.buf.rfind(" ", 0, self.max_chars))
            if cut > 20:
                return cut + 1
        return None

    def push(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while (cut := self._next_cut()) is not None:
            piece = self.buf[:cut].strip()
            self.buf = self.buf[cut:]
            if piece:
                out.append(piece)
                self.emitted += 1
        return out

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        if rest:
            self.emitted += 1
            return [rest]
        return []
