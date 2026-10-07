"""Clinic policy knowledge base: lexical TF-IDF retrieval over small markdown docs.

Deliberately no embedding call on the voice path: ~20 chunks, sub-millisecond lookups,
zero added latency. Swap for a vector store when the corpus grows.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("the a an and or of to for is are be at by in on with we our you your it this that".split())


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


class PolicyKB:
    def __init__(self, folder: Path | None = None):
        folder = folder or Path(__file__).parent / "kb"
        self.chunks: list[tuple[str, str]] = []
        for f in sorted(folder.glob("*.md")):
            for block in re.split(r"\n(?=# )", f.read_text()):
                block = block.strip()
                if block:
                    title = block.splitlines()[0].lstrip("# ").strip()
                    self.chunks.append((title, block))
        self._tf = [Counter(_tokens(b)) for _, b in self.chunks]
        df = Counter(w for tf in self._tf for w in tf)
        n = len(self.chunks)
        self._idf = {w: math.log((n + 1) / (c + 0.5)) for w, c in df.items()}

    def search(self, query: str, k: int = 2) -> list[dict]:
        q = _tokens(query)
        scored = []
        for i, tf in enumerate(self._tf):
            s = sum(tf[w] * self._idf.get(w, 0) for w in q) / (1 + math.log(1 + sum(tf.values())))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [{"title": self.chunks[i][0], "text": self.chunks[i][1]} for _, i in scored[:k]]
