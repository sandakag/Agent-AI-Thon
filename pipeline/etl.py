"""Transform + Load — the clean ETL data-plane (Extract already done upstream).

``parse``      raw trade -> ``{product, amount, ts, trade_id, side}`` where
               ``amount = price * size`` (USD value of the trade). A record
               whose price/size is missing or unparseable yields ``amount=None``
               (a NULL).
``aggregate``  revenue per product + a grand total.
``load``       idempotent upsert into a JSON "warehouse" keyed by
               ``(product, run_date)``.

Production failure condition (a latent resilience gap, exactly like a real
postmortem): the load stage refuses to publish when the batch is mostly NULL
amounts (``null_rate >= NULL_RATE_CRITICAL``) — otherwise revenue would
silently read $0 — so it fails. That is the incident the agent must predict.
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


def parse_trades(raw: list[dict]) -> list[dict]:
    parsed: list[dict] = []
    for r in raw:
        price = _to_float(r.get("price"))
        size = _to_float(r.get("size"))
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

    # Resilience fix: quarantine null-amount records and publish the VALID
    # subset instead of failing the whole batch, so a burst of bad upstream
    # records never zeroes revenue. Alert on the quarantined count.
    if total and null_rate >= config.NULL_RATE_CRITICAL:
        valid = [p for p in parsed if p["amount"] is not None]
        if valid:
            good = aggregate(valid)
            result["aggregate"] = good
            result["quarantined"] = nulls
            result["warehouse"] = load(good)
            result["error"] = (
                "quarantined %d null-amount records; published %d valid"
                % (nulls, len(valid))
            )
            return result
    if total == 0 or null_rate >= config.NULL_RATE_CRITICAL:
        result["failed"] = True
        result["error"] = (
            f"LoadFailure: null_rate={null_rate:.0%} >= "
            f"{config.NULL_RATE_CRITICAL:.0%} — refusing to publish $0 revenue"
        )
        return result

    result["warehouse"] = load(agg)
    return result
