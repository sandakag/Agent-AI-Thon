"""Vector memory (RAG) — the agent's "vector-based learning".

Stores each past prediction + realised outcome as a small token vector and
retrieves the most similar prior incidents for the agent to reason from. Uses
pure-Python cosine similarity so it runs with no key and no heavy deps; if
GitHub Models embeddings are wired later, the same ``add``/``retrieve``
interface applies. Memory grows over time, so accuracy and calibration improve
as real incidents accumulate.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

import config

_TOKEN = re.compile(r"[a-z0-9]+")


def _vec(text: str) -> dict:
    counts: dict[str, int] = {}
    for tok in _TOKEN.findall(text.lower()):
        counts[tok] = counts.get(tok, 0) + 1
    return counts


def _cos(a: dict, b: dict) -> float:
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    na = math.sqrt(sum(v * v for v in a.values())) or 1e-9
    nb = math.sqrt(sum(v * v for v in b.values())) or 1e-9
    return dot / (na * nb)


class VectorMemory:
    def __init__(self, path=None):
        self.path = path or config.MEMORY_FILE
        self.items: list[dict] = []
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self.items = []

    def add(self, text: str, metadata: dict) -> None:
        self.items.append(
            {
                "text": text,
                "metadata": metadata,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.items = self.items[-500:]  # bound the store
        self.path.write_text(json.dumps(self.items, indent=2))

    def retrieve(self, query: str, k: int = 3) -> list[dict]:
        if not self.items:
            return []
        qv = _vec(query)
        scored = [(_cos(qv, _vec(it["text"])), it) for it in self.items]
        scored.sort(key=lambda s: s[0], reverse=True)
        return [it for score, it in scored[:k] if score > 0.05]
