"""Opus-powered detailed Root-Cause Analysis for a predicted incident.

When the guardian predicts a failure it doesn't just raise a red banner — it asks
the high-power reasoning brain (Claude Opus 4.8 via the Copilot API) to produce a
*complete, structured RCA*: an executive summary, the most-likely root cause, a
long-form technical analysis, a tick-by-tick timeline, the blast radius / impact,
the evidence, the immediate on-call actions, and the preventive measures that stop
it recurring. The RCA is generated ONCE per incident (deduped by the loop) so the
real-time risk loop stays fast, and it degrades to a fully grounded, non-LLM RCA
when no Copilot credential is available — so it always renders something useful.
"""

from __future__ import annotations

import statistics

import config
from agent.brain_base import BrainError

_RCA_SYSTEM = (
    "You are a principal Site Reliability Engineer writing the definitive "
    "root-cause analysis for a data-pipeline incident that an AI guardian "
    "PREDICTED before it fired. You are given live telemetry, the grounded "
    "prediction and the recent signal trends. Produce a THOROUGH, specific, "
    "actionable RCA — this is read by on-call engineers and leadership. Ground "
    "every claim in the provided numbers; never invent telemetry. Be detailed in "
    "'detailed_analysis' (several paragraphs: what is happening, the mechanism, "
    "why it will breach, and the reasoning). Respond with ONLY a single JSON "
    "object (no prose, no code fences) using EXACTLY these keys: "
    "title (string), summary (string, 1-2 sentences), root_cause (string), "
    "detailed_analysis (string, multi-paragraph), timeline (array of strings), "
    "impact (string: SLA/revenue/customer blast radius), "
    "contributing_factors (array of strings), evidence (array of strings), "
    "immediate_actions (array of strings, what the on-call does NOW), "
    "preventive_measures (array of strings, how to stop recurrence), "
    "confidence (string: low|medium|high with one clause why)."
)

_KEYS = ("title", "summary", "root_cause", "detailed_analysis", "timeline",
         "impact", "contributing_factors", "evidence", "immediate_actions",
         "preventive_measures", "confidence")


def build_context(prediction: dict, signals_window: list[dict]) -> dict:
    """Compact, model-ready telemetry context from a rolling signal window."""
    window = [s for s in (signals_window or []) if isinstance(s, dict)][-12:]

    def series(field: str) -> list[float]:
        return [s[field] for s in window
                if isinstance(s.get(field), (int, float))]

    def slope(vals: list[float]) -> float:
        n = len(vals)
        if n < 3:
            return 0.0
        xs = list(range(n))
        mx = sum(xs) / n
        my = sum(vals) / n
        denom = sum((x - mx) ** 2 for x in xs) or 1e-9
        return sum((xs[i] - mx) * (vals[i] - my) for i in range(n)) / denom

    lat = series("latency_ms")
    latest = window[-1] if window else {}
    sla_soft, sla_hard = config.SLA_LATENCY_MS, config.LATENCY_TIMEOUT_MS
    cur_lat = lat[-1] if lat else None
    lat_slope = round(slope(lat[-8:]), 1) if len(lat) >= 3 else 0.0
    sla_state = "unknown"
    if cur_lat is not None:
        if cur_lat >= sla_hard:
            sla_state = "breaching hard timeout"
        elif cur_lat >= sla_soft:
            sla_state = "over soft SLA"
        elif lat_slope > 1:
            sla_state = "approaching SLA"
        else:
            sla_state = "within SLA"

    return {
        "prediction": {
            "predicted_failure_type": prediction.get("predicted_failure_type"),
            "risk_score": prediction.get("risk_score"),
            "pipeline_health": prediction.get("pipeline_health"),
            "confidence": prediction.get("confidence"),
            "lead_time_minutes": prediction.get("lead_time_minutes"),
            "evidence": prediction.get("evidence"),
            "recommended_action": prediction.get("recommended_action"),
            "reasoned_by": prediction.get("source"),
        },
        "sla": {
            "current_latency_ms": cur_lat,
            "soft_sla_ms": sla_soft,
            "hard_timeout_ms": sla_hard,
            "latency_trend_ms_per_tick": lat_slope,
            "state": sla_state,
            "null_rate_critical": config.NULL_RATE_CRITICAL,
            "min_records": config.MIN_RECORDS,
        },
        "latest_signals": latest,
        "recent_latency_ms": lat[-8:],
        "recent_null_rate": series("null_rate")[-8:],
        "recent_record_count": series("record_count")[-8:],
    }


def generate_rca(prediction: dict, signals_window: list[dict], brain) -> dict:
    """Return a structured RCA dict, Opus-authored when a brain is available."""
    ctx = build_context(prediction, signals_window)
    if brain is not None and getattr(brain, "available", False):
        import json
        user = ("Write the RCA for this predicted incident.\n\nTelemetry (JSON):\n"
                + json.dumps(ctx, default=str))
        try:
            out = brain.chat_json(_RCA_SYSTEM, user, temperature=0.2)
            if isinstance(out, dict) and out.get("root_cause"):
                rca = {k: out.get(k) for k in _KEYS}
                rca["source"] = getattr(brain, "model", getattr(brain, "name", "llm"))
                rca["predicted_failure_type"] = prediction.get("predicted_failure_type")
                rca["risk_score"] = prediction.get("risk_score")
                return _coerce(rca)
        except BrainError:
            pass
        except Exception:  # noqa: BLE001 - RCA must never crash the caller
            pass
    return _fallback_rca(prediction, ctx)


def _coerce(rca: dict) -> dict:
    """Make sure list fields are lists and text fields are strings."""
    for k in ("timeline", "contributing_factors", "evidence",
              "immediate_actions", "preventive_measures"):
        v = rca.get(k)
        if isinstance(v, str):
            rca[k] = [v]
        elif not isinstance(v, list):
            rca[k] = []
    for k in ("title", "summary", "root_cause", "detailed_analysis", "impact",
              "confidence"):
        if rca.get(k) is None:
            rca[k] = ""
    return rca


def _fallback_rca(prediction: dict, ctx: dict) -> dict:
    """A fully grounded RCA built without any LLM (headless / no-credential)."""
    ft = prediction.get("predicted_failure_type") or "pipeline degradation"
    sla = ctx["sla"]
    ev = list(prediction.get("evidence") or [])
    lead = prediction.get("lead_time_minutes")
    cur = sla.get("current_latency_ms")
    trend = sla.get("latency_trend_ms_per_tick")
    is_latency = "latency" in ft.lower() or "timeout" in ft.lower()

    timeline = ["Signals were inside their learned normal band (healthy warm-up)."]
    if is_latency and cur is not None:
        timeline += [
            f"Processing latency began climbing ~{trend} ms/tick.",
            f"Latency reached {cur:.0f} ms — {sla['state']} "
            f"(soft {sla['soft_sla_ms']:.0f} ms / hard {sla['hard_timeout_ms']:.0f} ms).",
            "Projected to cross the hard timeout, at which the load stage aborts "
            "the batch (the predicted break).",
        ]
    else:
        timeline += ["A monitored signal drifted past its normal band and the "
                     "forecaster projected a breach of an operating limit."]

    analysis = (
        f"The guardian predicts a {ft} at risk {prediction.get('risk_score')}/100 "
        f"with ~{lead} min of lead time. "
        + (f"Processing latency is {cur:.0f} ms and rising ~{trend} ms/tick against a "
           f"{sla['soft_sla_ms']:.0f} ms soft SLA and a {sla['hard_timeout_ms']:.0f} ms "
           "hard timeout; on the current trajectory it will cross the timeout and the "
           "load stage will abort the batch. "
           if is_latency and cur is not None else
           "A monitored signal has sustained a statistically significant deviation "
           "from its learned baseline and is trending toward an operating limit. ")
        + "The prediction is raised BEFORE the failure line so there is time to act."
    )

    return _coerce({
        "title": f"Predicted {ft}",
        "summary": (f"{ft} predicted at risk {prediction.get('risk_score')}/100 with "
                    f"~{lead} min lead time."),
        "root_cause": (prediction.get("recommended_action") and
                       f"Likely cause: sustained drift in the driving signal. "
                       f"{_root_cause_for(ft)}") or _root_cause_for(ft),
        "detailed_analysis": analysis,
        "timeline": timeline,
        "impact": _impact_for(ft),
        "contributing_factors": ev or ["sustained deviation in the driving signal"],
        "evidence": ev or ["forecaster flagged a sustained, one-directional drift"],
        "immediate_actions": _actions_for(ft),
        "preventive_measures": _prevention_for(ft),
        "confidence": f"{prediction.get('confidence')} (grounded heuristic)",
        "source": "grounded-heuristic",
        "predicted_failure_type": ft,
        "risk_score": prediction.get("risk_score"),
    })


def _root_cause_for(ft: str) -> str:
    f = (ft or "").lower()
    if "latency" in f or "timeout" in f or "load" in f:
        return ("Rising load is pushing ETL processing time up super-linearly; "
                "consumers cannot keep pace so latency ramps toward the timeout.")
    if "schema" in f:
        return "An upstream field was renamed/reshaped, so the strict parser drops rows."
    if "null" in f or "quality" in f:
        return "Missing/malformed values are inflating the null-amount rate."
    if "volume" in f or "throughput" in f or "stall" in f:
        return "Upstream production/consumer lag is starving the batch."
    return "A monitored signal is drifting one-directionally past its normal band."


def _impact_for(ft: str) -> str:
    f = (ft or "").lower()
    if "latency" in f or "timeout" in f:
        return ("SLA breach then a hard processing timeout that aborts the batch — "
                "the ETL stops loading, so downstream revenue/aggregates stall.")
    if "null" in f or "quality" in f:
        return "Revenue is under-reported (null amounts) and can fall to $0 at the load gate."
    return "Downstream aggregates/revenue become stale or incomplete."


def _actions_for(ft: str) -> list[str]:
    f = (ft or "").lower()
    if "latency" in f or "timeout" in f or "load" in f:
        return ["Scale out ETL consumers and raise parallelism now.",
                "Enable backpressure and chunk the batch to cap per-batch work.",
                "Shed or defer non-critical load until latency is back under the SLA."]
    if "schema" in f:
        return ["Add the renamed field alias to the parser.",
                "Quarantine unparseable records; reprocess after the alias lands."]
    if "null" in f or "quality" in f:
        return ["Quarantine + repair malformed records before load.",
                "Hold the load if null-rate approaches the critical gate."]
    return ["Confirm the deviation against recent upstream/deploy changes.",
            "Contain the source before it breaches the operating limit."]


def _prevention_for(ft: str) -> list[str]:
    f = (ft or "").lower()
    if "latency" in f or "timeout" in f or "load" in f:
        return ["Autoscale consumers on a latency SLO, not just CPU.",
                "Cap batch size and add admission control / backpressure by default.",
                "Load-test to the timeout and alert on the latency TREND, not the breach."]
    if "schema" in f:
        return ["Enforce a schema contract / registry with the upstream producer.",
                "Alert on schema-hash drift before rows start dropping."]
    if "null" in f or "quality" in f:
        return ["Add producer-side validation for required fields.",
                "Gate the load on a null-rate SLO with early-warning thresholds."]
    return ["Track each signal against its learned baseline and alert on drift early.",
            "Add guardrails at the relevant operating limit."]


def render_markdown(rca: dict) -> str:
    """Compact Markdown rendering of an RCA (for GitHub issues / logs)."""
    def bullets(items):
        return "\n".join(f"- {i}" for i in (items or [])) or "- (none)"
    return (
        f"## 🔬 AI Root-Cause Analysis — {rca.get('title', 'Predicted incident')}\n"
        f"_Reasoned by `{rca.get('source', 'llm')}`_\n\n"
        f"**Summary:** {rca.get('summary', '')}\n\n"
        f"**Root cause:** {rca.get('root_cause', '')}\n\n"
        f"### Detailed analysis\n{rca.get('detailed_analysis', '')}\n\n"
        f"### Timeline\n{bullets(rca.get('timeline'))}\n\n"
        f"### Impact\n{rca.get('impact', '')}\n\n"
        f"### Contributing factors\n{bullets(rca.get('contributing_factors'))}\n\n"
        f"### Evidence\n{bullets(rca.get('evidence'))}\n\n"
        f"### Immediate actions\n{bullets(rca.get('immediate_actions'))}\n\n"
        f"### Preventive measures\n{bullets(rca.get('preventive_measures'))}\n\n"
        f"**Confidence:** {rca.get('confidence', '')}\n"
    )
