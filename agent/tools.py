"""Deterministic grounding tools the agent calls so its risk / confidence are
backed by real numbers (better calibration, less hallucination). Pure stdlib —
this is the "scikit-learn style" tool layer, kept dependency-free for the demo.

Detection is signal-agnostic: instead of a hand-written rule per fault, EVERY
numeric signal is fed through the same online forecasting model (see
``agent/forecaster.py``). The model learns each signal's own normal, scores how
surprised it is by the latest value (onset), and projects the trajectory to a
breach (imminence). Risk therefore reacts to the statistics of ANY signal —
including incidents the system was never taught — and it fires BEFORE the
failure line is crossed. The per-signal names/labels below are EXPLANATIONS of
whichever signal moved, not the thing that triggers detection.
"""

from __future__ import annotations

import statistics

import config
from agent import forecaster

# Human labels for each monitored signal — used only to EXPLAIN which signal
# drove the risk, never to trigger it.
_MONITOR_LABEL = {
    "null_rate": "data-quality (null-rate)",
    "latency_ms": "processing latency",
    "record_count": "throughput / volume",
    "revenue": "revenue",
    "throughput_rps": "throughput rate",
    "distinct_products": "product coverage",
    "lag_seconds": "event lag / freshness",
    "dup_rate": "duplicate rate",
    "source_errors": "source errors",
}

# The continuous signals the forecaster models for FAILURE detection. Only
# signals that are stable on healthy live data and are actually driven by a
# pipeline fault belong here: null-rate (data quality), latency (processing SLA),
# record volume (throughput/starvation) and duplicate rate (at-least-once storms).
# Revenue / throughput-rate / freshness-lag / product-coverage are DERIVED or
# naturally noisy on a live market feed, so treating them as failure signals
# produced false-positive incidents (e.g. a "revenue anomaly" from a normal market
# dip while every pipeline metric was healthy). They stay as DISPLAYED metrics but
# no longer trigger an incident on their own.
_NUMERIC_SIGNALS = (
    "null_rate", "latency_ms", "record_count", "dup_rate",
)

# Real failure lines. Used ONLY to convert a drift into a time-to-breach — they
# are operating limits for the "ticks-to-breach" story, not the detector.
_HARD_LIMITS = {
    "null_rate": (None, config.NULL_RATE_CRITICAL),
    "latency_ms": (None, config.LATENCY_TIMEOUT_MS),
    "record_count": (config.MIN_RECORDS, None),
}

_HORIZON = 12.0    # ticks ahead we still count as "imminent"
_CONFIRM_SIGMA = 4.0   # a signal is "anomalous" only past this many robust-sigma

# Which way is "bad" for each signal — the SAME orientation every monitoring
# tool encodes: latency / null-rate / lag / duplicates are dangerous when they
# rise; revenue / throughput / record volume / product coverage are dangerous
# when they FALL. A move in the safe direction (revenue rising, latency easing)
# is never a pipeline failure, so it must not raise risk. This is per-signal
# ORIENTATION, not per-fault tuning — it says nothing about any specific fault.
_DANGER_UP = frozenset({"null_rate", "latency_ms", "lag_seconds", "dup_rate"})


def trend_slope(series: list[float]) -> float:
    """Least-squares slope of a series vs its index. 0 if fewer than 3 points."""
    n = len(series)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(series) / n
    denom = sum((x - mx) ** 2 for x in xs) or 1e-9
    return sum((xs[i] - mx) * (series[i] - my) for i in range(n)) / denom


def r_squared(series: list[float]) -> float:
    """Goodness-of-fit of the linear trend (used for confidence)."""
    n = len(series)
    if n < 3:
        return 0.0
    slope = trend_slope(series)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(series) / n
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in series) or 1e-9
    ss_res = sum((series[i] - (slope * xs[i] + intercept)) ** 2 for i in range(n))
    return max(0.0, 1 - ss_res / ss_tot)


def ticks_to_threshold(series: list[float], threshold: float):
    """Project how many ticks until a rising series reaches ``threshold``."""
    if not series:
        return None
    cur = series[-1]
    slope = trend_slope(series)
    if cur >= threshold:
        return 0
    if slope <= 1e-6:
        return None  # not trending toward it
    return max(0.0, (threshold - cur) / slope)


def robust_z_last(series: list[float]) -> float:
    """Robust z-score (median / MAD) of the latest point vs its recent history.

    Lets the agent flag ANY signal behaving abnormally — even one the fixed
    detectors were never taught — so a novel, never-before-seen incident still
    raises risk and wakes the reasoning brain.
    """
    if len(series) < 5:
        return 0.0
    hist = series[:-1]
    cur = series[-1]
    med = statistics.median(hist)
    mad = statistics.median([abs(x - med) for x in hist])
    if mad > 1e-9:
        return 0.6745 * (cur - med) / mad
    sd = statistics.pstdev(hist)
    if sd > 1e-9:
        return (cur - med) / sd
    return 0.0


def _deviation(value: float, ref_median: float, ref_scale: float) -> float:
    """Robust deviation of ``value`` from a signal's stable reference. When the
    reference is effectively constant, any change is treated as a hard deviation
    so stepped / constant-valued signals are still caught."""
    if ref_scale > 1e-9:
        return (value - ref_median) / ref_scale
    if abs(value - ref_median) <= 1e-9:
        return 0.0
    return 10.0 if value > ref_median else -10.0


def ground(collector) -> dict:
    """Signal-agnostic, forecasting-based grounding.

    Every numeric signal is run through the SAME online model (see
    ``agent/forecaster.py``): learn its own normal, score the forecast
    ``surprise`` of the latest value (onset of a deviation), and project the
    trajectory to a breach of an operating limit (imminence). The risk is fused
    from whichever signals moved — so a NOVEL incident on ANY signal is caught,
    and it is caught *before* the failure line is crossed. The per-signal labels
    below only EXPLAIN which signal drove the risk; they don't trigger it.

    The returned dict is used both as the transparent fallback prediction AND as
    the grounded evidence handed to the reasoning brain, so the LLM never invents
    risk from nothing.
    """
    latest = collector.history[-1] if collector.history else {}
    hist_len = len(collector.history)

    drivers: list[tuple] = []       # (score, field, direction, z, ttb)
    anomaly_fields: list[str] = []
    lead_candidates: list[float] = []
    top_z = 0.0

    # --- The general detector: identical maths for every continuous signal ---
    # Each signal is scored two ways, learned purely from its OWN past:
    #   onset     — how far outside its established normal the latest value sits
    #   imminence — how soon its smoothed trajectory will breach a real limit
    # An anomaly must PERSIST for >=3 ticks (same direction) before it counts, so
    # a transient market blip (e.g. a one-off whale trade that inflates revenue
    # for a tick or two) on noisy live data never raises a false alarm, while a
    # real fault (which ramps over many ticks) is still caught early.
    for fld in _NUMERIC_SIGNALS:
        s = collector.series(fld)
        if len(s) < 6:
            continue  # not enough history yet to know this signal's normal

        # Stable reference = history EXCLUDING the last 3 (possibly-anomalous)
        # points, so a fresh spike/step can't poison its own baseline.
        ref = s[:-3] if len(s) >= 9 else s[:-1]
        rmed = statistics.median(ref)
        rscale = forecaster.robust_scale(ref)
        dev_last = _deviation(s[-1], rmed, rscale)
        dev_prev = _deviation(s[-2], rmed, rscale)
        dev_prev2 = _deviation(s[-3], rmed, rscale)
        az = abs(dev_last)
        direction = "rising" if s[-1] >= rmed else "falling"

        # DIRECTION: only a move TOWARD degradation is a risk. A revenue / volume
        # / throughput RISE or a null / latency DROP is a healthy move, never a
        # failure — ignoring it is what keeps live market swings GREEN.
        dangerous = (dev_last > 0) if fld in _DANGER_UP else (dev_last < 0)
        if not dangerous:
            continue

        # CONFIRMATION: only a deviation SUSTAINED for 3 consecutive ticks in the
        # SAME direction counts. A transient 1-2 tick blip (market noise / a lone
        # whale trade) is ignored; a real fault ramps over many ticks and passes.
        if not (az >= _CONFIRM_SIGMA
                and abs(dev_prev) >= 3.0 and abs(dev_prev2) >= 3.0
                and (dev_last > 0) == (dev_prev > 0) == (dev_prev2 > 0)):
            continue

        # (a) ONSET — how far outside its own learned normal the signal now sits.
        onset = min(1.0, 0.4 + (az - _CONFIRM_SIGMA) / 6.0)

        # (b) IMMINENCE — project the smoothed trajectory to a real failure line;
        #     yields the ticks-to-breach lead time that makes this predictive.
        imm = 0.0
        ttb = None
        level, trend, _ = forecaster.holt(s)
        lo, hi = _HARD_LIMITS.get(fld, (None, None))
        if lo is not None or hi is not None:
            ttb = forecaster.ticks_to_band(level, trend, lo, hi)
            if ttb is not None and ttb <= _HORIZON:
                imm = min(1.0, (_HORIZON - ttb) / _HORIZON)
                lead_candidates.append(ttb)

        score = min(100.0, 100.0 * (0.6 * onset + 0.6 * imm))
        drivers.append((score, fld, direction, az, ttb))
        anomaly_fields.append(fld)
        top_z = max(top_z, az)

    # Discrete failure FLAGS the smooth forecaster genuinely can't model (they
    # sit at a constant baseline until they fire) are surfaced directly. This is
    # not per-fault tuning — it just acknowledges non-continuous signals.
    source_errors = latest.get("source_errors") or 0
    if source_errors:
        drivers.append((min(100.0, 55.0 + 6.0 * source_errors),
                        "source_errors", "rising", 0.0, None))
        anomaly_fields.append("source_errors")

    drivers.sort(reverse=True)

    # --- Fuse: the worst signal sets the level; corroborating signals add on ---
    risk = 0.0
    if drivers:
        worst = drivers[0][0]
        extra = sum(1 for d in drivers[1:] if d[0] >= 30.0)
        risk = worst + min(18.0, 7.0 * extra)

    # Schema drift is a discrete hash change; its numeric effect (null-rate) is
    # already caught above, but surface the categorical event too.
    schema_drift = bool(latest.get("schema_drift"))
    if schema_drift:
        risk = max(risk, 45.0)

    risk = float(max(0, min(99, round(risk))))

    # --- Explanation: describe whichever signals actually drove the risk ---
    evidence: list[str] = []
    if schema_drift:
        evidence.append(
            "schema hash changed vs baseline (upstream field renamed / added / dropped)"
        )
    for score, fld, direction, az, ttb in drivers[:4]:
        if fld == "source_errors":
            evidence.append(f"source fetch errors firing (now {source_errors})")
            continue
        msg = (f"{_MONITOR_LABEL[fld]} {direction} abnormally "
               f"(forecast surprise z\u2248{az:.1f}, now {latest.get(fld)}")
        if ttb is not None:
            msg += f"; ~{ttb:.1f} ticks to its limit"
        msg += ")"
        evidence.append(msg)
    if not evidence:
        evidence = ["all signals within their learned normal range"]

    failure_type = _classify(drivers, schema_drift)
    lead_ticks = min(lead_candidates) if lead_candidates else None

    # Confidence: how well-defined the driving signal's recent trajectory is,
    # plus how much history we've accumulated.
    fit = r_squared(collector.series(drivers[0][1])[-12:]) if drivers else 0.0
    confidence = round(min(0.95, 0.45 + 0.35 * fit + min(0.15, hist_len / 100.0)), 2)

    # Legacy display features (kept for the dashboard; no longer drive risk).
    null_series = collector.series("null_rate")
    lat_series = collector.series("latency_ms")
    null_slope = trend_slope(null_series)
    lat_slope = trend_slope(lat_series)
    lat_ttf = ticks_to_threshold(lat_series, config.SLA_LATENCY_MS)

    return {
        "risk_score": risk,
        "predicted_failure_type": failure_type,
        "lead_ticks": lead_ticks,
        "confidence": confidence,
        "evidence": evidence,
        "features": {
            "null_rate": latest.get("null_rate"),
            "null_slope_per_tick": round(null_slope, 4),
            "null_trend_r2": round(r_squared(null_series), 2),
            "record_count": latest.get("record_count"),
            "schema_drift": schema_drift,
            "lag_seconds": latest.get("lag_seconds"),
            "latency_ms": latest.get("latency_ms"),
            "latency_slope_ms_per_tick": round(lat_slope, 1),
            "ticks_to_latency_sla": round(lat_ttf, 1) if lat_ttf is not None else None,
            "dup_rate": latest.get("dup_rate"),
            "anomaly_driver": drivers[0][1] if drivers else None,
            "anomaly_signals": anomaly_fields,
            "anomaly_z": round(top_z, 1),
        },
    }


def _classify(drivers: list[tuple], schema_drift: bool) -> str:
    """Name the failure from whichever signal dominated the fused risk. This is
    an EXPLANATION of the general detector's output, not what triggered it."""
    if schema_drift:
        return "schema-drift / parse failure -> $0 revenue"
    if not drivers:
        return "none"
    top = drivers[0][1]
    if top == "null_rate":
        return "data-quality / null-rate surge -> $0 revenue"
    if top == "latency_ms":
        return "latency degradation / processing timeout under load"
    if top in ("record_count", "throughput_rps"):
        return "upstream stall / throughput collapse"
    if top == "source_errors":
        return "source outage / upstream API degraded"
    return f"emerging anomaly ({_MONITOR_LABEL[top]} {drivers[0][2]})"
