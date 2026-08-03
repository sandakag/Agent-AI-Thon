"""Always-on LIVE data tap for the Predictive Guardian demo.

Polls the real Coinbase public trades API every cycle and streams each trade
onto Kafka, so the pipeline is visibly alive in real time (watch the topic
counts climb in Kafka-UI):

    Coinbase live trades  --LOAD-->  Kafka `trades-raw`         (EXTRACT source)
    revenue per product   --LOAD-->  Kafka `trades-aggregated`  (final sink)

Each cycle also writes the extract -> transform -> load story to a lock-free
telemetry file (`audit/stream.jsonl`) that Promtail ships to Loki, so Grafana
shows the live heartbeat next to the predictive KPIs.

Run (inside the airflow image, which has kafka-python-ng):
    python /opt/airflow/project/airflow/stream_generator.py

Env knobs:
    STREAM_INTERVAL_SECONDS   seconds between cycles          (default 5)
    KAFKA_BOOTSTRAP           broker address                  (default kafka:29092)
    PRODUCTS                  comma list of Coinbase products (default from config)
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable

_PROJECT_ROOT = os.environ.get("ETL_PROJECT_ROOT") or str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config  # noqa: E402
from ingestion.coinbase_source import fetch_batch  # noqa: E402
from pipeline.etl import parse_trades, aggregate  # noqa: E402

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:29092")
TOPIC_RAW = os.environ.get("KAFKA_TOPIC_RAW", "trades-raw")
TOPIC_AGG = os.environ.get("KAFKA_TOPIC_AGG", "trades-aggregated")
INTERVAL = float(os.environ.get("STREAM_INTERVAL_SECONDS", "5"))
_STREAM_LOG = Path(_PROJECT_ROOT) / "audit" / "stream.jsonl"

_running = True


def _stop(*_a):
    global _running
    _running = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def stream_emit(event: str, **fields) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event,
           "source": "stream", **fields}
    try:
        with _STREAM_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _make_producer() -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: str(k).encode("utf-8") if k is not None else None,
        acks="all",
        retries=3,
        linger_ms=50,
    )


def main() -> int:
    print(f"[stream] connecting to Kafka at {BOOTSTRAP} ...", flush=True)
    producer = None
    for attempt in range(1, 31):
        try:
            producer = _make_producer()
            break
        except NoBrokersAvailable:
            print(f"[stream] broker not ready (attempt {attempt}/30), retrying...", flush=True)
            time.sleep(2)
    if producer is None:
        print("[stream] FATAL: could not connect to Kafka broker.", flush=True)
        return 1

    print(
        f"[stream] LIVE. interval={INTERVAL}s products={config.PRODUCTS} "
        f"raw->{TOPIC_RAW} agg->{TOPIC_AGG}\n"
        f"[stream] Watch topics fill live in Kafka-UI: http://localhost:18085",
        flush=True,
    )

    cycle = total_raw = total_agg = 0
    try:
        while _running:
            cycle += 1
            batch = fetch_batch()
            for rec in batch:
                producer.send(TOPIC_RAW, key=rec.get("trade_id"), value=rec)

            parsed = parse_trades(batch)
            agg = aggregate(parsed)
            run_date = datetime.now(timezone.utc).isoformat()
            agg_rows = [
                {"product": p, "total": v, "run_date": run_date}
                for p, v in sorted(agg["per_product"].items())
            ]
            for row in agg_rows:
                producer.send(TOPIC_AGG, key=row["product"], value=row)
            producer.flush(timeout=10)

            try:
                stream_emit("stream_extract_ok", count=len(batch), cycle=cycle)
                stream_emit("stream_transform_ok", rows=len(agg_rows),
                            revenue=agg["total_revenue"], cycle=cycle)
                stream_emit("stream_load_ok", published=len(agg_rows), cycle=cycle)
            except Exception:  # noqa: BLE001
                pass

            total_raw += len(batch)
            total_agg += len(agg_rows)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            top = ", ".join(f"{r['product']}={r['total']}" for r in agg_rows[:4])
            print(
                f"[{ts}] cycle {cycle:>4} | EXTRACT {len(batch):>3} trades -> "
                f"TRANSFORM {len(agg_rows)} products -> LOAD {len(agg_rows)} "
                f"| revenue[{top}] | lifetime raw={total_raw} agg={total_agg}",
                flush=True,
            )
            time.sleep(INTERVAL)
    finally:
        if producer is not None:
            producer.flush(timeout=10)
            producer.close(timeout=10)
        print(f"[stream] stopped after {cycle} cycles "
              f"(raw={total_raw}, agg={total_agg}).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
