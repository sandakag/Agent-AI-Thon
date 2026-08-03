"""Deterministic grounding tools the agent calls so its risk / confidence are
backed by real numbers (better calibration, less hallucination). Pure stdlib —
this is the "scikit-learn style" tool layer, kept dependency-free for the demo.
"""

from __future__ import annotations

import statistics

import config


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


def ground(collector) -> dict:
    """Compute grounded feature numbers + a transparent heuristic risk.

    Returned dict is used both as the fallback prediction AND as the evidence
    handed to the GitHub Models brain so it never invents risk from nothing.
    """
    null_series = collector.series("null_rate")
    vol_series = collector.series("record_count")
    lat_series = collector.series("latency_ms")
    latest = collector.history[-1] if collector.history else {}

    null_slope = trend_slope(null_series)
    null_r2 = r_squared(null_series)
    ttf_ticks = ticks_to_threshold(null_series, config.NULL_RATE_CRITICAL)

    lat_slope = trend_slope(lat_series)
    lat_r2 = r_squared(lat_series)
    lat_ttf = ticks_to_threshold(lat_series, config.SLA_LATENCY_MS)
    cur_lat = latest.get("latency_ms", 0.0) or 0.0

    evidence: list[str] = []
    risk = 0.0

    # 1) Data-quality trend toward the load-failure line
    cur_null = latest.get("null_rate", 0.0) or 0.0
    if cur_null > 0:
        risk += min(40.0, cur_null / config.NULL_RATE_CRITICAL * 40.0)
    if null_slope > 0.002 and ttf_ticks is not None:
        risk += 30.0
        evidence.append(
            f"null-rate rising (slope={null_slope:.3f}/tick, now {cur_null:.0%}, "
            f"~{ttf_ticks:.1f} ticks to the {config.NULL_RATE_CRITICAL:.0%} load-fail line)"
        )

    # 2) Schema drift
    if latest.get("schema_drift"):
        risk += 35.0
        evidence.append("schema drift vs baseline (upstream field renamed/added)")

    # 3) Volume collapse / starvation
    if vol_series:
        base = statistics.median(vol_series)
        cur_vol = vol_series[-1]
        if base and cur_vol < base * 0.5:
            risk += 25.0
            evidence.append(f"throughput collapse ({cur_vol} vs median {base:.0f} records)")
        if cur_vol < config.MIN_RECORDS:
            risk += 15.0
            evidence.append(f"record count {cur_vol} below floor {config.MIN_RECORDS}")

    # 4) Latency / load trend toward the processing-timeout SLA (early warning)
    if cur_lat > 0:
        risk += min(35.0, cur_lat / config.SLA_LATENCY_MS * 35.0)
    if lat_slope > 1.0 and lat_ttf is not None:
        risk += 25.0
        evidence.append(
            f"latency rising under load (slope={lat_slope:.0f}ms/tick, now "
            f"{cur_lat:.0f}ms, ~{lat_ttf:.1f} ticks to the "
            f"{config.SLA_LATENCY_MS:.0f}ms SLA)"
        )
    if cur_lat > config.SLA_LATENCY_MS:
        risk += 15.0
        evidence.append(
            f"latency over SLA ({cur_lat:.0f}ms > {config.SLA_LATENCY_MS:.0f}ms)"
        )

    # 5) Source health / duplicates
    if latest.get("source_errors"):
        risk += 20.0
        evidence.append("source fetch errors (upstream API degraded)")
    if latest.get("dup_rate", 0) > 0.1:
        risk += 10.0
        evidence.append(f"duplicate deliveries ({latest['dup_rate']:.0%})")

    risk = float(max(0, min(99, round(risk))))

    # predicted failure type
    if latest.get("schema_drift") or (null_slope > 0.002 and cur_null > 0):
        failure_type = "schema-drift / parse failure -> $0 revenue"
    elif (lat_slope > 1.0 and lat_ttf is not None) or cur_lat > config.SLA_LATENCY_MS:
        failure_type = "latency degradation / processing timeout under load"
    elif vol_series and vol_series[-1] < config.MIN_RECORDS:
        failure_type = "upstream stall / throughput collapse"
    elif latest.get("source_errors"):
        failure_type = "source outage"
    else:
        failure_type = "none"

    # lead time = the SOONEST projected breach across the data-quality and the
    # latency/load trends (whichever failure is predicted to arrive first)
    candidates = [t for t in (ttf_ticks, lat_ttf) if t not in (None, 0)]
    lead_ticks = min(candidates) if candidates else None

    # confidence from the strongest trend fit + amount of accumulated history
    hist = len(collector.history)
    fit = max(null_r2, lat_r2)
    confidence = round(min(0.95, 0.4 + 0.4 * fit + min(0.15, hist / 100.0)), 2)

    return {
        "risk_score": risk,
        "predicted_failure_type": failure_type,
        "lead_ticks": lead_ticks,
        "confidence": confidence,
        "evidence": evidence or ["all signals within normal range"],
        "features": {
            "null_rate": round(cur_null, 4),
            "null_slope_per_tick": round(null_slope, 4),
            "null_trend_r2": round(null_r2, 2),
            "record_count": latest.get("record_count"),
            "schema_drift": latest.get("schema_drift", False),
            "lag_seconds": latest.get("lag_seconds"),
            "latency_ms": latest.get("latency_ms"),
            "latency_slope_ms_per_tick": round(lat_slope, 1),
            "ticks_to_latency_sla": round(lat_ttf, 1) if lat_ttf is not None else None,
            "dup_rate": latest.get("dup_rate"),
        },
    }
