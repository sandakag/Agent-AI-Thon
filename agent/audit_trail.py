"""Tamper-evident, hash-chained audit trail (dependency-free).

Every governed action the guardian takes — a prediction, an early warning, a
predicted-incident issue, a gated preventive PR, a resolution — is appended as
an immutable, hash-chained JSONL record to ``config.AUDIT_LOG``. Each line binds
to the previous one via ``hash = sha256(prev_hash + payload)``, so any edit to
an earlier line breaks the chain and is detected by :func:`verify_chain` (which
the live dashboard surfaces as an integrity badge).

A cross-process lock lets the always-on stream and the Airflow tasks append
concurrently without interleaving and corrupting the chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

import config

_LOCK_PATH = str(config.AUDIT_LOG) + ".lock"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _acquire_lock(timeout: float = 5.0) -> int | None:
    deadline = time.time() + timeout
    while True:
        try:
            return os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(_LOCK_PATH) > 10:
                    os.unlink(_LOCK_PATH)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.02)


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
        os.unlink(_LOCK_PATH)
    except OSError:
        pass


def _last_hash() -> str:
    """Hash of the last record — O(1), reads only the file tail."""
    if not config.AUDIT_LOG.exists():
        return "GENESIS"
    try:
        with config.AUDIT_LOG.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return "GENESIS"
            block, data, pos = 4096, b"", size
            while pos > 0:
                read = min(block, pos)
                pos -= read
                fh.seek(pos)
                data = fh.read(read) + data
                lines = [ln for ln in data.split(b"\n") if ln.strip()]
                if lines and (pos == 0 or data.count(b"\n") >= 2):
                    try:
                        return json.loads(lines[-1].decode("utf-8")).get("hash", "GENESIS")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
            return "GENESIS"
    except OSError:
        return "GENESIS"


def audit(event: str, **fields) -> dict:
    """Append one immutable, hash-chained audit record (lock-guarded)."""
    record = {"ts": _now(), "event": event, **fields}
    payload = json.dumps(record, sort_keys=True)
    fd = _acquire_lock()
    try:
        prev = _last_hash()
        record["prev"] = prev
        record["hash"] = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
        with config.AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    finally:
        _release_lock(fd)
    return record


def load_events(limit: int | None = None) -> list[dict]:
    if not config.AUDIT_LOG.exists():
        return []
    events: list[dict] = []
    for line in config.AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events[-limit:] if limit else events


def verify_chain(events: list[dict] | None = None) -> tuple[bool, int]:
    """Recompute the chain; return ``(intact, first_broken_index | -1)``."""
    events = events if events is not None else load_events()
    prev = "GENESIS"
    for i, rec in enumerate(events):
        body = {k: v for k, v in rec.items() if k not in ("prev", "hash")}
        payload = json.dumps(body, sort_keys=True)
        expected = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
        if rec.get("hash") != expected or rec.get("prev") != prev:
            return False, i
        prev = rec["hash"]
    return True, -1


def stream_emit(event: str, **fields) -> None:
    """Append a lock-free live-heartbeat record to the stream telemetry file
    (Promtail tails it into Loki). Never used for governed actions."""
    rec = {"ts": _now(), "event": event, "source": "stream", **fields}
    try:
        with config.STREAM_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass
