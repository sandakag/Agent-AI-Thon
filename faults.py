"""Demo fault injection — gradually degrade the LIVE feed so we can prove the
agent predicts a failure BEFORE it happens (a measurable lead time).

Every fault RAMPS UP with the tick number: the corrupted fraction grows over
time, so a health signal (schema, null-rate, volume) drifts slowly and the
predictive agent's risk climbs *ahead of* the hard failure threshold.

Modes
-----
``none``          pristine live data
``schema-drift``  upstream renames ``price`` -> ``px`` for a growing fraction
``null-surge``    ``size`` set to null for a growing fraction (missing quantity)
``volume-drop``   the batch shrinks tick over tick (upstream stall / starvation)
``latency-surge`` load climbs tick over tick, so processing latency ramps past the
                  SLA and finally a hard timeout ceiling (the pipeline breaks under
                  load). The batch itself is untouched — the latency (and the
                  eventual timeout) is modeled in :func:`load_latency`.
"""

from __future__ import annotations

import random

import config


def apply_fault(batch: list[dict], mode: str, tick: int,
                inject_at: int = 3, ramp: float = 0.12) -> list[dict]:
    """Return a (possibly) corrupted copy of ``batch`` for the given tick."""
    if mode in (None, "none") or tick < inject_at:
        return batch
    if mode == "latency-surge":
        return batch          # load-induced latency is modeled in load_latency()

    frac = min(0.9, (tick - inject_at + 1) * ramp)  # grows each tick

    if mode == "volume-drop":
        keep = max(1, int(len(batch) * (1.0 - frac)))
        return batch[:keep]

    corrupted: list[dict] = []
    for rec in batch:
        r = dict(rec)
        if random.random() < frac:
            if mode == "schema-drift" and "price" in r:
                r["px"] = r.pop("price")   # renamed field defeats the parser
            elif mode == "null-surge":
                r["size"] = None           # missing quantity -> null amount
        corrupted.append(r)
    return corrupted


def modeled_latency_ms(mode: str, tick: int, inject_at: int = 1) -> float:
    """Extra processing latency (ms) injected by a load fault at ``tick``.

    Models rising load: the effective ETL latency climbs super-linearly each
    tick, crossing the soft SLA (an early, predictable trend) and finally the
    hard timeout ceiling where the load stage aborts. Deterministic and instant
    — no real sleeping, so the live demo stays fast.
    """
    if mode != "latency-surge" or tick < inject_at:
        return 0.0
    step = tick - inject_at + 1
    return 700.0 * step + 180.0 * step * step


def load_latency(mode: str, tick: int, real_latency_ms: float,
                 inject_at: int = 1) -> tuple[float, str | None]:
    """Return ``(effective_latency_ms, timeout_error)`` for the given tick.

    ``effective_latency_ms`` is the measured latency plus any load-induced
    latency. ``timeout_error`` is a message when the effective latency crosses
    the hard ``LATENCY_TIMEOUT_MS`` ceiling (the pipeline broke under load),
    otherwise ``None``.
    """
    effective = float(real_latency_ms) + modeled_latency_ms(mode, tick, inject_at)
    error = None
    if mode == "latency-surge" and effective >= config.LATENCY_TIMEOUT_MS:
        error = (
            f"processing timeout under load — {effective:.0f}ms >= "
            f"{config.LATENCY_TIMEOUT_MS:.0f}ms ceiling (load stage aborted the batch)"
        )
    return effective, error
