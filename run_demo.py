"""Predictive Pipeline Guardian — end-to-end demo runner.

Loop (once per tick):
    EXTRACT live Coinbase trades
      -> (optional ramping fault injection)
      -> ETL (transform + load)
      -> collect health signals
      -> predictive agent (GitHub Models + grounding tools + vector memory)
      -> preventive policy
      -> early-warning / governance output

At the end it reports the LEAD TIME between the first AMBER warning and the
real failure — the headline metric (how early we caught it).

Usage
-----
    python run_demo.py                          # pristine live data (stays GREEN)
    python run_demo.py --inject schema-drift    # predict a failure before it happens
    python run_demo.py --inject null-surge --ticks 20 --interval 4
    python run_demo.py --inject volume-drop
    python run_demo.py --inject latency-surge    # load climbs -> latency SLA breach -> timeout
"""

from __future__ import annotations

import argparse
import time

import config
from agent.predictive_agent import PredictiveAgent
from alerting.notifier import clear_incident, emit
from faults import apply_fault, load_latency
from ingestion.coinbase_source import fetch_batch
from pipeline.etl import run_etl
from policy.policy_engine import decide
from signals.collector import SignalCollector


def main() -> None:
    ap = argparse.ArgumentParser(description="Predictive Pipeline Guardian demo")
    ap.add_argument("--ticks", type=int, default=15)
    ap.add_argument("--interval", type=float, default=4.0, help="seconds between ticks")
    ap.add_argument(
        "--inject",
        choices=["none", "schema-drift", "null-surge", "volume-drop", "latency-surge"],
        default="none",
    )
    ap.add_argument("--inject-at", type=int, default=3, help="tick to start the fault")
    args = ap.parse_args()

    collector = SignalCollector()
    agent = PredictiveAgent()

    brain = (
        f"{agent.llm.name} ({agent.llm.model})"
        if agent.llm.available
        else "HEURISTIC fallback (approved AI brain unavailable — install the GitHub "
        "Copilot CLI, or set BRAIN in .env)"
    )
    print(f"Source   : Coinbase live trades {config.PRODUCTS}")
    print(f"AI brain : {brain}")
    print(f"Scenario : inject={args.inject}  ticks={args.ticks}  interval={args.interval}s")
    print("-" * 92)

    first_warning: int | None = None
    first_failure: int | None = None

    for tick in range(1, args.ticks + 1):
        t0 = time.time()
        raw = fetch_batch()
        raw = apply_fault(raw, args.inject, tick, inject_at=args.inject_at)
        etl = run_etl(raw)
        latency_ms = (time.time() - t0) * 1000.0
        latency_ms, load_error = load_latency(
            args.inject, tick, latency_ms, inject_at=args.inject_at
        )
        if load_error and not etl["failed"]:
            etl["failed"] = True
            etl["error"] = load_error

        signals = collector.collect(raw, etl, latency_ms)
        prediction = agent.predict(collector, args.interval)
        decision = decide(prediction)
        emit(tick, prediction, decision)

        if decision["should_alert"] and first_warning is None and not etl["failed"]:
            first_warning = tick
        if etl["failed"]:
            print(f"    !!! ETL FAILED: {etl['error']}")
            if first_failure is None:
                first_failure = tick
            agent.learn(signals, prediction, outcome="failed")
        else:
            agent.learn(signals, prediction, outcome="ok")

        time.sleep(max(0.0, args.interval))

    print("-" * 92)
    if first_warning and first_failure and first_failure > first_warning:
        lead = (first_failure - first_warning) * args.interval
        print(
            f"PREDICTED EARLY: first warning at tick {first_warning}, real failure "
            f"at tick {first_failure}  ->  LEAD TIME ~ {lead:.0f}s "
            f"({first_failure - first_warning} ticks before it broke)."
        )
    elif first_warning and not first_failure:
        print(
            f"Early warnings raised from tick {first_warning}; failure PREVENTED "
            "(never crossed the fail line)."
        )
    elif args.inject == "none":
        print("Healthy run — pipeline stayed GREEN on live data.")
    else:
        print("Run complete.")
    clear_incident()


if __name__ == "__main__":
    main()
