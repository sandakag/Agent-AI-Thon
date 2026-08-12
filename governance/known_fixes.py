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


# --- Real per-incident code repairs -----------------------------------------
# Each repair adds ONE genuine resilience construct to the live parser, so the
# guardian's staged PR is a REAL code fix (not a comment) that a human reviews
# and merges to harden production.
_RESOLVE_ALIAS_HELPER = (
    "\n\ndef _resolve_alias(record: dict, names: tuple):\n"
    "    \"\"\"Return the first present alias of a (possibly renamed) upstream field.\"\"\"\n"
    "    for _n in names:\n"
    "        if record.get(_n) is not None:\n"
    "            return record.get(_n)\n"
    "    return None\n"
)
_HARD_PRICE = '        price = _to_float(_resolve_alias(r, ("price", "px", "p", "prc")))'
_HARD_SIZE = '        size = _to_float(_resolve_alias(r, ("size", "qty", "quantity", "sz")))'
_DEDUP_BLOCK = (
    "    # Idempotency fix: drop at-least-once duplicate redeliveries by trade_id\n"
    "    # so a replay storm never double-counts revenue.\n"
    "    _seen, _deduped = set(), []\n"
    "    for _p in parsed:\n"
    "        _tid = _p.get(\"trade_id\")\n"
    "        if _tid is not None and _tid in _seen:\n"
    "            continue\n"
    "        _seen.add(_tid)\n"
    "        _deduped.append(_p)\n"
    "    parsed = _deduped\n"
)
_QUARANTINE_BLOCK = (
    "    # Resilience fix: quarantine null-amount records and publish the VALID\n"
    "    # subset instead of failing the whole batch, so a burst of bad records\n"
    "    # never zeroes revenue.\n"
    "    if total and null_rate >= config.NULL_RATE_CRITICAL:\n"
    "        valid = [p for p in parsed if p[\"amount\"] is not None]\n"
    "        if valid:\n"
    "            good = aggregate(valid)\n"
    "            result[\"aggregate\"] = good\n"
    "            result[\"quarantined\"] = nulls\n"
    "            result[\"warehouse\"] = load(good)\n"
    "            result[\"error\"] = (\n"
    "                \"quarantined %d null-amount records; published %d valid\"\n"
    "                % (nulls, len(valid))\n"
    "            )\n"
    "            return result\n"
)


def _fix_schema(src: str) -> str | None:
    """Add upstream field-alias resolution (px/quantity/…) to parse_trades."""
    import re
    changed = False
    if "_resolve_alias" not in src:
        anchor = "    except (TypeError, ValueError):\n        return None\n"
        if anchor in src:
            src = src.replace(anchor, anchor + _RESOLVE_ALIAS_HELPER, 1)
            changed = True
    new = re.sub(r"        price = _to_float\([^\n]*\)", _HARD_PRICE, src, count=1)
    if new != src:
        src, changed = new, True
    new = re.sub(r"        size = _to_float\([^\n]*\)", _HARD_SIZE, src, count=1)
    if new != src:
        src, changed = new, True
    return src if changed else None


def _fix_dup(src: str) -> str | None:
    """Add trade_id de-duplication so a replay/dup storm cannot double-count."""
    if "_deduped" in src:
        return None
    anchor = "    parsed = parse_trades(raw)\n"
    if anchor not in src:
        return None
    return src.replace(anchor, anchor + _DEDUP_BLOCK, 1)


def _fix_null(src: str) -> str | None:
    """Add null-amount quarantine so a null/malformed surge cannot zero revenue."""
    if 'result["quarantined"]' in src:
        return None
    anchor = "    if total == 0 or null_rate >= config.NULL_RATE_CRITICAL:"
    if anchor not in src:
        return None
    return src.replace(anchor, _QUARANTINE_BLOCK + "\n" + anchor, 1)


# Map an incident's failure-type keyword to its REAL code repair. A recognized
# incident with no targeted repair falls back to the full hardened restore.
_REPAIRS = (
    ("schema", _fix_schema),
    ("parse", _fix_schema),
    ("duplicate", _fix_dup),
    ("dup", _fix_dup),
    ("null", _fix_null),
    ("quality", _fix_null),
    ("corrupt", _fix_null),
    ("type", _fix_null),
)


def deterministic_fix(failure_type: str, current_content: str) -> tuple[str, str] | None:
    """Author a REAL per-incident code repair for a recognized runtime incident.

    Adds the specific missing defense — alias resolution / de-duplication / null
    quarantine — as genuine code (not a comment). Returns None when the failure
    type is unknown OR the defense is already present (so no duplicate PR).
    A recognized incident with no targeted repair falls back to restoring the
    full hardened parser (still a real code change)."""
    label = _label_for(failure_type)
    if label is None:
        return None  # unknown incident -> let a generative model try instead
    current = current_content or ""
    ft = (failure_type or "").lower()
    for key, repair in _REPAIRS:
        if key in ft:
            fixed = repair(current)
            if fixed is None:
                return None  # defense already present -> nothing to change
            try:
                compile(fixed, "pipeline/etl.py", "exec")
            except SyntaxError:
                break  # fall through to full restore
            return (fixed if fixed.endswith("\n") else fixed + "\n"), label

    hardened = hardened_source()
    if not hardened or hardened.strip() == current.strip():
        return None
    return (hardened if hardened.endswith("\n") else hardened + "\n"), label
