"""Transform + Load — the VULNERABLE baseline data-plane (demo reset target).

This is the intentionally un-hardened parser the demo starts from: it reads
``price``/``size`` with NO alias resolution (schema-drift breaks it), does NO
de-duplication (a dup storm double-counts) and NO null quarantine (a null surge
hard-fails the batch). The guardian predicts the incident and stages a PR that
restores ``pipeline/etl_hardened.py`` — the fully-hardened version — which a
human merges to heal production. ``reset_stacks`` copies THIS file back over
``pipeline/etl.py`` so the next audience starts from the same vulnerable state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import config


class LoadFailure(RuntimeError):
    """The load stage aborted rather than publish corrupt ($0) revenue."""


def _to_float(v):
    try:
        f = float(v)
        return f if f == f else None  # reject NaN
    except (TypeError, ValueError):
        return None


def _resolve_alias(record: dict, names: tuple):
    """Return the first present alias of a (possibly renamed) upstream field."""
    for _n in names:
        if record.get(_n) is not None:
            return record.get(_n)
    return None


def parse_trades(raw: list[dict]) -> list[dict]:
    parsed: list[dict] = []
    for r in raw:
        price = _to_float(_resolve_alias(r, ("price", "px", "p", "prc")))
        size = _to_float(_resolve_alias(r, ("size", "qty", "quantity", "sz")))
        amount = price * size if (price is not None and size is not None) else None
        parsed.append(
            {
                "product": r.get("product", "unknown"),
                "trade_id": r.get("trade_id"),
                "amount": amount,
                "side": r.get("side"),
                "ts": r.get("time"),
            }
        )
    return parsed


def aggregate(parsed: list[dict]) -> dict:
    per_product: dict[str, float] = {}
    valid = 0
    for row in parsed:
        amt = row["amount"]
        if amt is None:
            continue
        valid += 1
        per_product[row["product"]] = round(per_product.get(row["product"], 0.0) + amt, 2)
    total = round(sum(per_product.values()), 2)
    return {"per_product": per_product, "total_revenue": total, "valid_records": valid}


def load(agg: dict, run_date: str | None = None) -> dict:
    run_date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    warehouse: dict = {}
    if config.WAREHOUSE_FILE.exists():
        try:
            warehouse = json.loads(config.WAREHOUSE_FILE.read_text())
        except json.JSONDecodeError:
            warehouse = {}
    for product, revenue in agg["per_product"].items():
        key = f"{product}|{run_date}"
        warehouse[key] = round(warehouse.get(key, 0.0) + revenue, 2)
    config.WAREHOUSE_FILE.write_text(json.dumps(warehouse, indent=2))
    return warehouse


def run_etl(raw: list[dict]) -> dict:
    """Full parse -> aggregate -> load. Returns a result dict; sets
    ``failed=True`` and skips the load when the batch is too NULL to publish."""
    parsed = parse_trades(raw)
    # VULNERABLE BASELINE: no de-duplication (a replay/dup storm double-counts
    # revenue) and no null quarantine (a null/schema-drift surge hard-fails the
    # whole batch). The guardian's staged fix restores these defenses.
    total = len(parsed)
    nulls = sum(1 for p in parsed if p["amount"] is None)
    null_rate = (nulls / total) if total else 1.0
    agg = aggregate(parsed)

    result = {
        "parsed": parsed,
        "aggregate": agg,
        "record_count": total,
        "null_count": nulls,
        "null_rate": round(null_rate, 4),
        "failed": False,
        "error": None,
    }

    if total == 0 or null_rate >= config.NULL_RATE_CRITICAL:
        result["failed"] = True
        result["error"] = (
            f"LoadFailure: null_rate={null_rate:.0%} >= "
            f"{config.NULL_RATE_CRITICAL:.0%} — refusing to publish $0 revenue"
        )
        return result

    result["warehouse"] = load(agg)
    return result
