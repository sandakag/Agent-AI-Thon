"""Deterministic ML / known-issue repair analyzer — the FIRST-line Phase 1 CI
repair brain.

Phase 1's demo CI fault is deliberately isolated to ``pipeline/pricing.py``, a
module independent of ``pipeline/etl.py`` (Phase 2's runtime-incident
vulnerability surface). This runs BEFORE any generative model: it recognizes
the previously-diagnosed failing-test signature and replays the exact,
already-verified fix. Instant and offline — no model call, no rate limit — and
because it only ever touches ``pricing.py``, healing it can never re-harden the
``pipeline/etl.py`` vulnerability Phase 2 needs.
"""
from __future__ import annotations

import difflib
from pathlib import Path

from agent.brain_base import BrainError

ROOT = Path(__file__).resolve().parents[1]
_TARGET = "pipeline/pricing.py"
_SIGNATURE = "test_round_amount_rounds_to_cents"
_BROKEN = "    return round(amount)\n"
_FIXED = "    return round(amount, 2)\n"


class PlaybookBrain:
    """Deterministic known-fix analyzer for the Phase 1 CI demo fault."""

    name = "ml-known-fix-analyzer"
    available = True

    def chat(self, system: str, user: str, *, temperature: float = 0.1) -> str:
        if _SIGNATURE not in user:
            raise BrainError("No known-fix analyzer entry matches this failure log.")
        target = ROOT / _TARGET
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise BrainError(f"pipeline/pricing.py unavailable: {exc}") from exc
        if _BROKEN not in current:
            raise BrainError("pipeline/pricing.py does not contain the known fault; nothing to fix.")
        new_content = current.replace(_BROKEN, _FIXED, 1)
        hunk = "".join(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{_TARGET}", tofile=f"b/{_TARGET}",
            )
        )
        return f"diff --git a/{_TARGET} b/{_TARGET}\n{hunk}"

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        raise BrainError("PlaybookBrain only supports unified-diff repairs.")
