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
``custom``        an OPEN-ENDED incident ANYONE can author at runtime: a list of
                  composable ``ops`` (null / drop / rename / scale / freeze /
                  make-constant / corrupt a field, shrink the batch, duplicate
                  rows, add latency), each ramping with the tick. The agent never
                  sees the spec — only the resulting data/latency — so it must
                  PREDICT a failure it was never told about. See
                  :func:`apply_custom` for the op vocabulary.
"""

from __future__ import annotations

import random

import config


def apply_fault(batch: list[dict], mode: str, tick: int,
                inject_at: int = 3, ramp: float = 0.12,
                spec: dict | None = None, recovery: float = 1.0) -> list[dict]:
    """Return a (possibly) corrupted copy of ``batch`` for the given tick.

    ``recovery`` (0..1) scales the fault DOWN — 1.0 = full injected fault, 0.0 =
    fully healed. The dashboard's "Apply fix" action decays this toward 0 over a
    few ticks so the operator watches the pipeline visibly recover."""
    if mode in (None, "none") or tick < inject_at:
        return batch
    if mode == "latency-surge":
        return batch          # load-induced latency is modeled in load_latency()
    if mode == "custom":
        return apply_custom(batch, spec or {}, tick, inject_at=inject_at,
                            ramp=ramp, recovery=recovery)

    frac = min(0.9, (tick - inject_at + 1) * ramp) * max(0.0, recovery)  # grows each tick

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
                 inject_at: int = 1, spec: dict | None = None,
                 recovery: float = 1.0) -> tuple[float, str | None]:
    """Return ``(effective_latency_ms, timeout_error)`` for the given tick.

    ``effective_latency_ms`` is the measured latency plus any load-induced
    latency (from the ``latency-surge`` mode or a ``custom`` incident's
    ``latency`` op). ``timeout_error`` is a message when the effective latency
    crosses the hard ``LATENCY_TIMEOUT_MS`` ceiling (the pipeline broke under
    load), otherwise ``None``.
    """
    added = modeled_latency_ms(mode, tick, inject_at)
    if mode == "custom":
        added += custom_latency_ms(spec or {}, tick, inject_at)
    added *= max(0.0, recovery)
    effective = float(real_latency_ms) + added
    error = None
    if added > 0 and effective >= config.LATENCY_TIMEOUT_MS:
        error = (
            f"processing timeout under load — {effective:.0f}ms >= "
            f"{config.LATENCY_TIMEOUT_MS:.0f}ms ceiling (load stage aborted the batch)"
        )
    return effective, error


def custom_latency_ms(spec: dict, tick: int, inject_at: int = 1) -> float:
    """Total load-induced latency (ms) from a custom incident's ``latency`` ops."""
    if not spec or tick < inject_at:
        return 0.0
    step = tick - inject_at + 1
    total = 0.0
    for op in spec.get("ops") or []:
        if isinstance(op, dict) and str(op.get("op", "")).lower() == "latency":
            ms = float(op.get("ms", 0) or 0)
            total += ms * step if op.get("ramp", True) else ms
    return total


# --- Open-ended custom incident engine -------------------------------------
# Anyone can author an incident as a list of composable ``ops`` on ANY field.
# The transforms are pure DATA mutations (no eval / no code execution), so a spec
# is safe to accept from an untrusted audience member, yet the combinatorial
# space is effectively unbounded — the agent sees only the resulting signals and
# has to PREDICT a failure it was never told about.


def apply_custom(batch: list[dict], spec: dict, tick: int,
                 inject_at: int = 1, ramp: float = 0.12,
                 recovery: float = 1.0) -> list[dict]:
    """Apply a user-authored incident ``spec`` (a list of ramping ``ops``).

    ``spec = {"label": "...", "ops": [ {"op": ..., "field": ..., ...}, ... ]}``.
    Supported ops (each ramps with the tick; optional ``intensity`` 0..1):

    * ``null_field``    set ``field`` to null for a growing fraction
    * ``drop_field``    delete ``field`` for a growing fraction
    * ``rename_field``  rename ``field`` -> ``to`` (defeats a strict parser)
    * ``scale_field``   multiply numeric ``field`` by ``factor`` (outliers/spikes)
    * ``freeze_field``  pin ``field`` to its first value (stale / stuck feed)
    * ``constant_field``set ``field`` to a fixed ``value``
    * ``corrupt_type``  set ``field`` to a non-numeric ``value`` (type break)
    * ``shrink_batch``  keep only ``(1-frac)`` of the rows (volume collapse)
    * ``duplicate``     re-deliver a fraction of rows (at-least-once storm)
    * ``latency``       add ``ms`` of processing latency (see custom_latency_ms)
    """
    ops = spec.get("ops") if isinstance(spec, dict) else None
    if not ops or tick < inject_at:
        return batch
    frac_base = min(0.95, (tick - inject_at + 1) * ramp) * max(0.0, recovery)
    out = [dict(r) for r in batch]
    for op in ops:
        if isinstance(op, dict):
            out = _apply_op(out, op, frac_base)
    return out


def _apply_op(records: list[dict], op: dict, frac_base: float) -> list[dict]:
    """Apply one incident op to the batch (per-record ops ramp with frac_base)."""
    kind = str(op.get("op", "")).lower()
    if kind in ("latency", "", "none"):
        return records                     # latency handled in custom_latency_ms()

    intensity = float(op.get("intensity", 1.0) or 1.0)
    frac = max(0.0, min(0.95, frac_base * intensity))

    if kind == "shrink_batch":
        keep = max(1, int(len(records) * (1.0 - frac)))
        return records[:keep]
    if kind == "duplicate":
        return records + [dict(r) for r in records if random.random() < frac]

    field = op.get("field")
    if not field:
        return records

    const = None
    if kind == "freeze_field":
        for r in records:
            if r.get(field) is not None:
                const = r[field]
                break

    out: list[dict] = []
    for rec in records:
        r = dict(rec)
        if random.random() < frac:
            if kind == "null_field":
                r[field] = None
            elif kind == "drop_field":
                r.pop(field, None)
            elif kind == "rename_field":
                if field in r:
                    r[str(op.get("to") or f"{field}_x")] = r.pop(field)
            elif kind == "scale_field":
                try:
                    r[field] = float(r.get(field)) * float(op.get("factor", 10))
                except (TypeError, ValueError):
                    pass
            elif kind == "constant_field":
                r[field] = op.get("value")
            elif kind == "freeze_field":
                if const is not None:
                    r[field] = const
            elif kind == "corrupt_type":
                r[field] = op.get("value", "ERR")
        out.append(r)
    return out
