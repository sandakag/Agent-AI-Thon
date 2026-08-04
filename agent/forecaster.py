"""Signal-agnostic forecasting anomaly engine (pure stdlib, no ML deps).

Every numeric health signal is modelled the SAME way — there is no per-fault
rule anywhere in here. For each signal we learn its OWN notion of "normal"
online with Holt double-exponential smoothing (a level term + a trend term),
then answer two questions the same way for every signal:

  * surprise_z(series)   -> how far the newest value fell outside the model's
                            one-step-ahead forecast, standardised by the signal's
                            own residual scale. Large |z| means "the model did
                            NOT expect this" — it catches the ONSET of any
                            deviation, including a fault the system was never
                            taught about.

  * ticks_to_band(...)   -> how many ticks until the smoothed trajectory
                            (level advancing by `trend` each tick) crosses an
                            operating limit. This turns "something is drifting"
                            into "it will breach in ~N ticks" — a genuine
                            *pre-failure* prediction rather than an after-the-fact
                            alarm.

Because the maths is identical for every signal, a user can perturb ANYTHING in
the stream and the detector reacts to the statistics, not to a hard-coded
scenario. That is what makes the agent's detection general and learning rather
than scripted.
"""

from __future__ import annotations

import statistics


def robust_scale(values: list[float]) -> float:
    """Outlier-resistant spread: 1.4826 * MAD (≈ std for a normal), falling back
    to population std, then 0 when the history is effectively constant."""
    if len(values) < 2:
        return 0.0
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad > 1e-9:
        return 1.4826 * mad
    sd = statistics.pstdev(values)
    return sd if sd > 1e-9 else 0.0


def holt(series: list[float], alpha: float = 0.4, beta: float = 0.3):
    """Holt double-exponential smoothing.

    Returns ``(level, trend, residuals)`` where ``residuals[i]`` is the actual
    value minus the one-step-ahead forecast that was made one tick earlier — the
    stream of "how wrong was the model each step", which is what we score.
    ``level``/``trend`` are the latest smoothed state, used to project forward.
    """
    n = len(series)
    if n == 0:
        return 0.0, 0.0, []
    level = float(series[0])
    trend = float(series[1] - series[0]) if n >= 2 else 0.0
    residuals: list[float] = []
    for i in range(1, n):
        forecast = level + trend  # one-step-ahead prediction made at i-1
        residuals.append(float(series[i]) - forecast)
        prev_level = level
        level = alpha * float(series[i]) + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
    return level, trend, residuals


def surprise_z(series: list[float], alpha: float = 0.4, beta: float = 0.3,
               min_points: int = 6):
    """Standardised one-step forecast residual of the LATEST point.

    Returns ``(z, level, trend)``. ``|z|`` large ⇒ the model, trained purely on
    this signal's own past, is surprised by the newest value. Signal-agnostic:
    the same call works for null-rate, latency, revenue or anything else.
    """
    if len(series) < min_points:
        return 0.0, (float(series[-1]) if series else 0.0), 0.0
    level, trend, residuals = holt(series, alpha, beta)
    if len(residuals) < min_points - 1:
        return 0.0, level, trend
    last = residuals[-1]
    hist = residuals[:-1]
    med = statistics.median(hist)
    scale = robust_scale(hist)
    if scale <= 1e-9:
        return 0.0, level, trend
    return (last - med) / scale, level, trend


def ticks_to_band(level: float, trend: float, lo=None, hi=None, eps: float = 1e-9):
    """Ticks until a trajectory (``level`` advancing by ``trend`` per tick)
    crosses an operating band ``[lo, hi]``. Returns the soonest crossing (0 if
    already outside) or ``None`` if it is not heading toward any limit."""
    cands: list[float] = []
    if hi is not None:
        if level >= hi:
            cands.append(0.0)
        elif trend > eps:
            cands.append((hi - level) / trend)
    if lo is not None:
        if level <= lo:
            cands.append(0.0)
        elif trend < -eps:
            cands.append((level - lo) / (-trend))
    return min(cands) if cands else None
