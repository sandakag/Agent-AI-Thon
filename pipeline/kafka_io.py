"""Kafka I/O for the live-trades data-plane.

The always-on stream-generator publishes live Coinbase trades onto the
``trades-raw`` topic; the predictive ETL consumes them back. If Kafka is
unreachable (e.g. running the demo outside the Docker stack), both sides fall
back gracefully to a direct Coinbase fetch — so the guardian always runs
end-to-end, with or without the full stack.
"""

from __future__ import annotations

import json

import config

try:  # kafka-python-ng is installed in the Airflow image; optional locally
    from kafka import KafkaConsumer, KafkaProducer  # noqa: F401

    _KAFKA = True
except Exception:  # noqa: BLE001
    _KAFKA = False


def kafka_available() -> bool:
    return _KAFKA


def _producer():
    from kafka import KafkaProducer

    return KafkaProducer(
        bootstrap_servers=config.KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: str(k).encode("utf-8") if k is not None else None,
        acks="all",
        retries=3,
        linger_ms=50,
    )


def publish_trades(records: list[dict], topic: str | None = None) -> int:
    """Publish raw trade records to ``trades-raw``. No-op (returns 0) if Kafka
    is not available in this environment."""
    if not _KAFKA:
        return 0
    topic = topic or config.KAFKA_TOPIC_RAW
    producer = _producer()
    published = 0
    try:
        for rec in records:
            producer.send(topic, key=rec.get("trade_id"), value=rec)
            published += 1
        producer.flush(timeout=10)
    finally:
        producer.close(timeout=10)
    return published


def publish_aggregated(rows: list[dict], topic: str | None = None) -> int:
    """Publish aggregated revenue rows to ``trades-aggregated``."""
    if not _KAFKA:
        return 0
    topic = topic or config.KAFKA_TOPIC_AGG
    producer = _producer()
    published = 0
    try:
        for row in rows:
            producer.send(topic, key=row.get("product"), value=row)
            published += 1
        producer.flush(timeout=10)
    finally:
        producer.close(timeout=10)
    return published


def consume_trades(
    max_records: int | None = None,
    timeout_ms: int = 4000,
    topic: str | None = None,
) -> list[dict]:
    """Consume up to ``max_records`` recent trades from ``trades-raw``.

    Falls back to a direct Coinbase fetch when Kafka is unavailable or the topic
    is momentarily empty, so the ETL never starves on account of the transport.
    """
    topic = topic or config.KAFKA_TOPIC_RAW
    max_records = max_records or (config.TRADES_PER_PRODUCT * max(1, len(config.PRODUCTS)))
    if _KAFKA:
        try:
            from kafka import KafkaConsumer

            consumer = KafkaConsumer(
                topic,
                bootstrap_servers=config.KAFKA_BOOTSTRAP,
                auto_offset_reset="latest",
                enable_auto_commit=True,
                group_id="predictive-guardian",
                value_deserializer=lambda b: json.loads(b.decode("utf-8")),
                consumer_timeout_ms=timeout_ms,
            )
            out: list[dict] = []
            try:
                for msg in consumer:
                    out.append(msg.value)
                    if len(out) >= max_records:
                        break
            finally:
                consumer.close()
            if out:
                return out
        except Exception:  # noqa: BLE001 - fall through to the source of truth
            pass

    from ingestion.coinbase_source import fetch_batch

    return fetch_batch()
