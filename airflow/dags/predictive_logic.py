"""Pure task logic for the Predictive Guardian DAG (no Airflow import).

Data plane, per scheduled run (one "tick"):

    trades-raw (Kafka)  --EXTRACT-->  ETL transform+load  --SIGNALS-->
      predictive agent (GitHub Copilot brain + grounding + vector memory)
      --> preventive policy --> early warning + REAL governed issue / gated PR

Trends drive *early* prediction, so the signal window is persisted across runs
(``signal_history.json``) and a per-mode tick counter (``guardian_state.json``)
lets an injected fault RAMP UP over successive triggers until risk crosses AMBER
then RED — exactly the lead-time story, straight from the Airflow UI.

Fault injection from the Airflow UI — *Trigger DAG w/ config*:
    {}                            # healthy live data  -> stays GREEN, resets ramp
    {"inject": "schema-drift"}    # upstream renames price -> px   (ramps each run)
    {"inject": "null-surge"}      # missing size -> null amounts    (ramps each run)
    {"inject": "volume-drop"}     # upstream stall / starvation     (ramps each run)
    {"reset": true}               # clear the ramp + incident banner
"""

from __future__ import annotations

import json
import os
import time

import config
from agent import audit_trail
from agent.predictive_agent import PredictiveAgent
from alerting.notifier import clear_incident, emit
from faults import apply_fault
from pipeline import kafka_io
from pipeline.etl import run_etl
from policy.policy_engine import decide
from signals.collector import SignalCollector

TICK_SECONDS = int(os.environ.get("GUARDIAN_TICK_SECONDS", "600"))  # schedule cadence


# ---------------------------------------------------------------------------
# Persisted ramp state + signal window (so trends survive across DAG runs)
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    if config.GUARDIAN_STATE_FILE.exists():
        try:
            return json.loads(config.GUARDIAN_STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"mode": "none", "tick": 0, "baseline_schema": None}


def _save_state(state: dict) -> None:
    config.GUARDIAN_STATE_FILE.write_text(json.dumps(state, indent=2))


def _load_history() -> list[dict]:
    if config.SIGNAL_HISTORY_FILE.exists():
        try:
            return json.loads(config.SIGNAL_HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return []


def _save_history(history: list[dict]) -> None:
    config.SIGNAL_HISTORY_FILE.write_text(json.dumps(history[-40:], indent=2, default=str))


def _collector_from_history(history: list[dict], baseline: str | None) -> SignalCollector:
    c = SignalCollector()
    c.baseline_schema = baseline
    for sig in history[-c.history.maxlen :]:
        c.history.append(sig)
    return c


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
def do_extract(conf: dict, run_id: str) -> dict:
    """EXTRACT — consume live trades from Kafka (Coinbase fallback) and apply the
    ramping fault for the selected mode. Returns the raw batch + tick metadata."""
    if conf.get("reset"):
        _save_state({"mode": "none", "tick": 0, "baseline_schema": None})
        clear_incident()

    mode = str(conf.get("inject", "none") or "none")
    state = _load_state()
    if mode != state.get("mode"):
        state = {"mode": mode, "tick": 0, "baseline_schema": state.get("baseline_schema")}
    state["tick"] = int(state.get("tick", 0)) + 1
    _save_state(state)

    raw = kafka_io.consume_trades()
    # inject_at=1 so the ramp begins immediately on the first injected run
    raw = apply_fault(raw, mode, state["tick"], inject_at=1)
    audit_trail.stream_emit("guardian_extract", count=len(raw), mode=mode, tick=state["tick"],
                            transport="kafka" if kafka_io.kafka_available() else "coinbase")
    return {"raw": raw, "mode": mode, "tick": state["tick"]}


def do_transform_load(raw: list[dict], run_id: str) -> dict:
    """TRANSFORM + LOAD — run the ETL, publish aggregated rows to Kafka, compute
    health signals and persist them to the rolling window."""
    t0 = time.time()
    etl = run_etl(raw)
    latency_ms = (time.time() - t0) * 1000.0

    if not etl["failed"]:
        rows = [
            {"product": p, "total": v, "run_date": None}
            for p, v in etl["aggregate"]["per_product"].items()
        ]
        try:
            kafka_io.publish_aggregated(rows)
        except Exception:  # noqa: BLE001 - transport hiccup must not fail the ETL task
            pass

    state = _load_state()
    history = _load_history()
    collector = _collector_from_history(history, state.get("baseline_schema"))
    sig = collector.collect(raw, etl, latency_ms)
    if state.get("baseline_schema") is None and not etl["failed"]:
        state["baseline_schema"] = collector.baseline_schema
        _save_state(state)
    _save_history(list(collector.history))

    audit_trail.audit("etl_run", record_count=etl["record_count"],
                      null_rate=etl["null_rate"], revenue=etl["aggregate"]["total_revenue"],
                      latency_ms=round(latency_ms, 1),
                      failed=etl["failed"], error=etl["error"])
    return {"signals": sig, "etl_failed": etl["failed"], "etl_error": etl["error"]}


def do_predict(run_id: str) -> dict:
    """REASON — the predictive agent (Copilot brain) forecasts the risk."""
    state = _load_state()
    history = _load_history()
    collector = _collector_from_history(history, state.get("baseline_schema"))
    agent = PredictiveAgent()
    prediction = agent.predict(collector, TICK_SECONDS)
    audit_trail.audit("agent_reason", brain=agent.llm.name, model=agent.llm.model,
                      available=agent.llm.available, risk=prediction.get("risk_score"),
                      source=prediction.get("source"))
    return prediction


def do_govern(prediction: dict, etl_failed: bool, etl_error: str | None,
              signals: dict, run_id: str) -> dict:
    """DECIDE + GOVERN — grade the risk, raise the early warning, and (AMBER+)
    open the REAL predicted-incident issue / (RED) gated preventive PR."""
    decision = decide(prediction)
    # tick number is cosmetic in the console line here
    emit(_load_state().get("tick", 0), prediction, decision)

    if decision["level"] == "GREEN":
        clear_incident()

    # close the learning loop so the vector memory sharpens over time
    try:
        PredictiveAgent().learn(signals, prediction,
                                outcome="failed" if etl_failed else "ok")
    except Exception:  # noqa: BLE001
        pass

    if etl_failed:
        audit_trail.audit("etl_failed_after_prediction", level="RED", error=etl_error,
                          predicted_risk=prediction.get("risk_score"),
                          predicted_type=prediction.get("predicted_failure_type"))
    return decision
