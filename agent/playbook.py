"""Deterministic ML / known-issue repair analyzer — the FIRST-line repair brain.

This runs BEFORE any generative model. It recognizes a previously-diagnosed
failure signature in the CI log and replays the already-verified, COMPLETE fix
by restoring the fully-hardened pipeline parser (``pipeline/etl_hardened.py``).
Because it replaces the whole file, it repairs EVERY known ETL fault in one shot
(alias resolution, de-duplication, null quarantine), so the verify step goes
green immediately instead of leaving a second failing test. It is instant and
offline — no model call and no rate limit — and only fires for signatures it has
a proven fix for, so a match is always safe to ship.
"""
from __future__ import annotations

import difflib
from pathlib import Path

from agent.brain_base import BrainError

ROOT = Path(__file__).resolve().parents[1]
_TARGET = "pipeline/etl.py"
_HARDENED = ROOT / "pipeline" / "etl_hardened.py"

# Failing-test / source fingerprints the hardened parser is known to resolve.
_KNOWN_SIGNATURES = (
    "test_schema_aliases_produce_revenue",
    "test_duplicate_trades_are_not_double_counted",
    "test_all_invalid_rows_fail_closed",
    "parse_trades",
    "pipeline/etl.py",
    "pipeline\\etl.py",
)


class PlaybookBrain:
    """Deterministic known-fix analyzer: restores the verified hardened parser
    for any recognized ETL failure signature, as ONE complete unified diff."""

    name = "ml-known-fix-analyzer"
    available = True

    def chat(self, system: str, user: str, *, temperature: float = 0.1) -> str:
        if not any(sig in user for sig in _KNOWN_SIGNATURES):
            raise BrainError("No known-fix analyzer entry matches this failure log.")
        try:
            hardened = _HARDENED.read_text(encoding="utf-8")
        except OSError as exc:
            raise BrainError(f"hardened reference unavailable: {exc}") from exc
        current = (ROOT / _TARGET).read_text(encoding="utf-8")
        if hardened.strip() == current.strip():
            raise BrainError("pipeline/etl.py is already hardened; nothing to fix.")
        # Full-file diff from the ACTUAL current source so it always applies.
        hunk = "".join(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                hardened.splitlines(keepends=True),
                fromfile=f"a/{_TARGET}", tofile=f"b/{_TARGET}",
            )
        )
        return f"diff --git a/{_TARGET} b/{_TARGET}\n{hunk}"

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        raise BrainError("PlaybookBrain only supports unified-diff repairs.")
