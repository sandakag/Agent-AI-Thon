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
"""

from __future__ import annotations

import random


def apply_fault(batch: list[dict], mode: str, tick: int,
                inject_at: int = 3, ramp: float = 0.12) -> list[dict]:
    """Return a (possibly) corrupted copy of ``batch`` for the given tick."""
    if mode in (None, "none") or tick < inject_at:
        return batch

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
