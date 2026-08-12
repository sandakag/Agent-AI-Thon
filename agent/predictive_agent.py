"""The Predictive Agent — the agentic loop bound to GitHub Copilot.

    perceive (signals) -> recall (vector memory) -> ground (tools)
      -> reason (GitHub Copilot LLM) -> decide (structured prediction)
      -> learn (write the outcome back to memory)

If the Copilot brain isn't authenticated (e.g. headless containers), the agent
degrades gracefully to the transparent tool-based heuristic, so the whole demo
still runs end-to-end. The LLM never invents the risk from nothing: it is handed
the grounded numbers and the retrieved precedents, then asked to reason,
calibrate and explain.
"""

from __future__ import annotations

import json

import config
from agent import tools as grounding
from agent.brain import make_brain, BrainError
from agent.vector_memory import VectorMemory

SYSTEM = (
    "You are an SRE reliability agent that PREDICTS data-pipeline failures "
    "BEFORE they happen. You are given grounded health signals, deterministic "
    "tool measurements, and similar past incidents. Reason like a senior on-call "
    "engineer. Respond with STRICT JSON only, no prose, using exactly these keys: "
    "pipeline_health (int 0-100, 100=perfect), risk_score (int 0-100), "
    "confidence (float 0-1), predicted_failure_type (short string), "
    "lead_time_minutes (number; minutes until likely failure, 0 if none), "
    "evidence (array of short strings), recommended_action (short string). "
    "Base the risk on the provided numbers; never fabricate signals."
)


class PredictiveAgent:
    def __init__(self):
        self.llm = make_brain()
        self.memory = VectorMemory()

    def predict(self, collector, interval_seconds: float, use_llm: bool = True) -> dict:
        grounded = grounding.ground(collector)
        latest = collector.history[-1] if collector.history else {}

        # recall similar precedents (RAG)
        query = f"{grounded['predicted_failure_type']} " + " ".join(grounded["evidence"])
        memories = self.memory.retrieve(query, k=3)

        lead_minutes = (
            round(grounded["lead_ticks"] * interval_seconds / 60.0, 1)
            if grounded.get("lead_ticks")
            else 0.0
        )

        # heuristic prediction (also the graceful fallback)
        prediction = {
            "pipeline_health": int(max(1, 100 - grounded["risk_score"])),
            "risk_score": int(grounded["risk_score"]),
            "confidence": float(grounded["confidence"]),
            "predicted_failure_type": grounded["predicted_failure_type"],
            "lead_time_minutes": lead_minutes,
            "evidence": grounded["evidence"],
            "recommended_action": _default_action(grounded["predicted_failure_type"]),
            # This is an online, telemetry-driven forecasting fallback (not a
            # generative answer). It remains available when every AI provider is
            # offline, so detection, RCA and governance never stall for known
            # incidents.
            "source": "fallback-forecaster",
        }

        # Reason with the AI brain ONLY when the deterministic risk is already
        # elevated (>= amber). Healthy green ticks stay instant on the cheap
        # heuristic; the expensive agentic brain (Copilot ~tens of seconds/call)
        # is spent exactly where it matters — when a failure is actually being
        # predicted. This is how a real predictive on-call system rations its
        # model budget, and it keeps the live demo fast.
        risk_elevated = grounded["risk_score"] >= config.RISK_AMBER
        if use_llm and self.llm.available and risk_elevated:
            user = json.dumps(
                {
                    "current_signals": latest,
                    "grounded_features": grounded["features"],
                    "tool_risk_estimate": grounded["risk_score"],
                    "tool_predicted_failure": grounded["predicted_failure_type"],
                    "tool_lead_time_minutes": lead_minutes,
                    "similar_past_incidents": [m["text"] for m in memories],
                },
                default=str,
            )
            try:
                out = self.llm.chat_json(SYSTEM, user)
                if out and "risk_score" in out:
                    out.setdefault("evidence", grounded["evidence"])
                    out.setdefault("recommended_action", prediction["recommended_action"])
                    out.setdefault("lead_time_minutes", lead_minutes)
                    out.setdefault(
                        "pipeline_health", int(max(1, 100 - int(out.get("risk_score", 0))))
                    )
                    out["source"] = f"{self.llm.name}:{self.llm.model}"
                    prediction = out
            except BrainError:
                pass  # keep the heuristic prediction

        prediction["similar_incidents"] = [m["text"] for m in memories]
        return prediction

    def learn(self, signals: dict, prediction: dict, outcome: str) -> None:
        """Write the prediction + realised outcome back to vector memory so the
        agent gets sharper as real incidents accumulate."""
        text = (
            f"failure_type={prediction.get('predicted_failure_type')} "
            f"risk={prediction.get('risk_score')} outcome={outcome} "
            f"null_rate={signals.get('null_rate')} "
            f"schema_drift={signals.get('schema_drift')} "
            f"records={signals.get('record_count')}"
        )
        self.memory.add(
            text,
            {
                "risk": prediction.get("risk_score"),
                "predicted": prediction.get("predicted_failure_type"),
                "outcome": outcome,
            },
        )


def _default_action(failure_type: str) -> str:
    ft = (failure_type or "").lower()
    if "schema" in ft:
        return "Validate + resolve field aliases; quarantine bad records before load."
    if "latency" in ft or "timeout" in ft or "load" in ft:
        return (
            "Scale out consumers + raise ETL parallelism; add backpressure and chunk "
            "the batch so processing time stays under the SLA before the load-timeout "
            "breaks the pipeline."
        )
    if "stall" in ft or "throughput" in ft:
        return "Check upstream producer / consumer lag; scale consumers; backfill the window."
    if "outage" in ft or "source" in ft:
        return "Fail over to a backup source / cache; reconnect with backoff."
    if "null" in ft or "quality" in ft:
        return (
            "Quarantine + repair the malformed records and resolve field aliases before "
            "load; the batch is trending toward the null-rate line that zeroes revenue."
        )
    if "anomaly" in ft:
        return (
            "Investigate the flagged signal against recent upstream / deploy changes; "
            "confirm the deviation is real, then contain the source before it breaches."
        )
    return "Continue monitoring; no action needed."
