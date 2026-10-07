"""Split streaming LLM text into speakable sentences so TTS can start early."""

from __future__ import annotations

import re

_BOUNDARY = re.compile(r"[.!?](?=\s)")
_ABBREV = ("dr.", "mr.", "mrs.", "ms.", "st.", "ave.", "a.m.", "p.m.", "no.", "e.g.", "i.e.", "vs.")


class SentenceChunker:
    """Emit speakable chunks as early as possible. The FIRST chunk goes out as soon as there is a
    short sentence ("Got it.") or, failing that, a clause ending in a comma ("Sure, let me check,"),
    so text-to-speech can start while the model is still writing the rest."""

    def __init__(self, max_chars: int = 160, first_min_chars: int = 6, first_clause_chars: int = 24):
        self.buf = ""
        self.max_chars = max_chars
        self.first_min_chars = first_min_chars
        self.first_clause_chars = first_clause_chars
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
        if self.emitted == 0:
            for m in re.finditer(r",(?=\s)", self.buf):
                if len(self.buf[:m.end()].strip()) >= self.first_clause_chars:
                    return m.end()
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
