"""Deterministic production-fix authorer — the reliable "known-fix" engine.

When the live guardian predicts a CODE-class incident it asks GitHub Copilot to
author the repair for ``pipeline/etl.py``. Copilot can be unavailable, rate
limited (HTTP 429) or occasionally return an unusable answer. For a live demo
that must never stall, this module is the deterministic fallback: it recognizes
the predicted-failure signature and returns the already-verified hardened
parser (``pipeline/etl_hardened.py``), which defends against every injected
incident (schema drift, null surge, dup storm, outliers, volume/latency).

It is used ONLY as a fallback inside ``governance.github_gov._copilot_code_fix``
so the flow stays "AI authored the fix" when Copilot works, and still produces
a correct, human-mergeable PR when it does not.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_HARDENED = _ROOT / "pipeline" / "etl_hardened.py"

# Every incident the demo injects maps to the same verified hardened parser, so
# whichever failure the guardian predicts, the staged fix is known-good. The
# label is only used for the commit/PR description.
_FIX_LABELS = {
    "schema": "restore upstream field-alias resolution (px/quantity/…) in parse_trades",
    "null": "restore null-amount quarantine so a null surge cannot zero revenue",
    "dup": "restore trade_id de-duplication so a replay storm cannot double-count",
    "duplicate": "restore trade_id de-duplication so a replay storm cannot double-count",
    "latency": "restore batch resilience so a load/latency surge degrades gracefully",
    "throughput": "restore low-volume handling so an upstream stall cannot corrupt revenue",
    "stall": "restore low-volume handling so an upstream stall cannot corrupt revenue",
    "anomaly": "restore outlier-tolerant aggregation so a price spike cannot skew revenue",
    "outlier": "restore outlier-tolerant aggregation so a price spike cannot skew revenue",
    "stale": "restore feed-freshness handling so a frozen field cannot mislead revenue",
    "corrupt": "restore malformed-value quarantine so invalid source values cannot poison revenue",
    "type": "restore malformed-value quarantine so invalid source values cannot poison revenue",
    "source": "restore resilient source handling so upstream errors are contained",
}


def _label_for(failure_type: str) -> str | None:
    """The change description for a RECOGNIZED failure type, else None so a novel
    incident falls through to a generative model instead of being wrongly claimed."""
    ft = (failure_type or "").lower()
    for key, label in _FIX_LABELS.items():
        if key in ft:
            return label
    return None


def hardened_source() -> str | None:
    """The verified hardened ``pipeline/etl.py`` contents, or None if missing."""
    try:
        return _HARDENED.read_text(encoding="utf-8")
    except OSError:
        return None


def deterministic_fix(failure_type: str, current_content: str) -> tuple[str, str] | None:
    """Return ``(new_etl_py_contents, change_description)`` for a RECOGNIZED
    incident, or None when the failure type is unknown, no hardened reference
    exists, or the source is already hardened."""
    label = _label_for(failure_type)
    if label is None:
        return None  # unknown incident -> let a generative model try instead
    hardened = hardened_source()
    if not hardened:
        return None
    if hardened.strip() == (current_content or "").strip():
        return None  # already hardened — nothing to change
    return hardened if hardened.endswith("\n") else hardened + "\n", label
