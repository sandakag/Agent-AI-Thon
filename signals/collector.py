"""Signal collector / feature store — turns each ETL run into the health
signals the predictive agent reasons over: data-quality, volume, schema,
freshness/lag, throughput/latency and duplicates. Keeps a rolling window so
downstream tools can measure TRENDS — the basis for *early* prediction.
"""

from __future__ import annotations

import hashlib
from collections import deque
from datetime import datetime, timezone


def _schema_hash(raw: list[dict]) -> str:
    keys: set[str] = set()
    for r in raw[:50]:
        keys.update(k for k in r.keys() if not k.startswith("_"))
    return hashlib.sha256(",".join(sorted(keys)).encode()).hexdigest()[:12]


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


class SignalCollector:
    def __init__(self, window: int = 40):
        self.history: deque[dict] = deque(maxlen=window)
        self.baseline_schema: str | None = None

    def collect(self, raw: list[dict], etl_result: dict, latency_ms: float) -> dict:
        now = datetime.now(timezone.utc)
        parsed = etl_result["parsed"]
        count = etl_result["record_count"]

        schema = _schema_hash(raw)
        if self.baseline_schema is None and not etl_result["failed"]:
            self.baseline_schema = schema

        # freshness / lag: how old is the newest event we received
        ev_times = [t for t in (_parse_ts(p["ts"]) for p in parsed) if t]
        lag_seconds = (now - max(ev_times)).total_seconds() if ev_times else None

        # duplicates (an at-least-once redelivery signal)
        ids = [p["trade_id"] for p in parsed if p["trade_id"] is not None]
        dup_rate = 1 - (len(set(ids)) / len(ids)) if ids else 0.0

        source_errors = sum(1 for r in raw if r.get("_source_error"))

        sig = {
            "ts": now.isoformat(),
            "record_count": count,
            "null_rate": etl_result["null_rate"],
            "revenue": etl_result["aggregate"]["total_revenue"],
            "distinct_products": len({p["product"] for p in parsed}),
            "schema_hash": schema,
            "schema_drift": bool(self.baseline_schema and schema != self.baseline_schema),
            "lag_seconds": round(lag_seconds, 1) if lag_seconds is not None else None,
            "dup_rate": round(dup_rate, 4),
            "latency_ms": round(latency_ms, 1),
            "throughput_rps": round(count / (latency_ms / 1000.0), 1) if latency_ms > 0 else 0.0,
            "source_errors": source_errors,
            "etl_failed": etl_result["failed"],
        }
        self.history.append(sig)
        return sig

    def series(self, field: str) -> list:
        return [h[field] for h in self.history if h.get(field) is not None]
