"""Predictive Pipeline Guardian — always-on REAL-TIME prediction engine.

This is the live brain of the demo. Unlike ``run_demo.py`` (a fixed-length,
run-once script) this loop runs *forever* inside the Docker stack and is what
makes the dashboard actually react in real time:

    every tick (~GUARDIAN_LOOP_INTERVAL s):
        consume any audience-injected incident (data/pending_incident.json)
          -> EXTRACT live Coinbase trades
          -> apply the RAMPING fault (so a signal drifts a little more each tick)
          -> ETL (transform + load)  + modeled load-latency
          -> collect health signals (kept in memory across ticks -> trends)
          -> predictive agent forecasts the risk BEFORE the failure line
          -> preventive policy grades it GREEN / AMBER / RED
          -> emit: banner + hash-chained audit + live stream heartbeat

Because the collector history lives in memory, a fault injected from the
dashboard *ramps from the moment you click it* — you watch latency (or null
rate, or volume) climb, risk cross AMBER then RED with a real lead time, and a
governed incident open, all seconds before the pipeline would actually break.

The engine never needs the Copilot CLI: in a headless container the brain
degrades to the transparent, grounded heuristic, so predictions are always live.
Set ``GITHUB_TOKEN`` + ``GITHUB_REPOSITORY`` (and optionally ``BRAIN``) to light
up real governance / the LLM brain.

Run (added to docker-compose as the ``guardian-loop`` service):
    python guardian_loop.py

Env knobs:
    GUARDIAN_LOOP_INTERVAL   seconds between ticks            (default 6)
    GUARDIAN_INJECT          start with a built-in fault      (default none)
    GUARDIAN_INJECT_AT       warmup ticks before that fault   (default 6)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from agent import audit_trail
from agent import rca as rca_mod
from agent.predictive_agent import PredictiveAgent
from alerting.notifier import clear_incident, emit
from faults import apply_fault, load_latency
from governance import github_gov
from ingestion.coinbase_source import fetch_batch
import pipeline.etl as etl_mod
from pipeline.etl import run_etl
from policy.policy_engine import decide
from signals.collector import SignalCollector

INTERVAL = float(os.environ.get("GUARDIAN_LOOP_INTERVAL", "6"))
START_INJECT = str(os.environ.get("GUARDIAN_INJECT", "none") or "none")
START_INJECT_AT = int(os.environ.get("GUARDIAN_INJECT_AT", "6"))
# When the operator clicks "Apply fix" the injected fault decays to 0 over this
# many ticks, so they watch the pipeline visibly recover before it settles GREEN.
RECOVERY_TICKS = max(1, int(os.environ.get("GUARDIAN_RECOVERY_TICKS", "4")))

_PENDING = config.DATA_DIR / "pending_incident.json"
_RCA_HISTORY = config.DATA_DIR / "rca_history.json"
# How often (in ticks) to ask GitHub whether the human merged the gated fix PR.
MERGE_POLL_TICKS = max(1, int(os.environ.get("GUARDIAN_MERGE_POLL_TICKS", "2")))
_SOURCE_FILE = Path(__file__).resolve().parent / "pipeline" / "etl.py"

# The real-time risk loop runs on the fast grounded heuristic; the expensive
# Opus RCA is generated ONCE per incident in a background thread (deduped by the
# predicted-failure signature) so the loop never blocks on a model call.
_rca_state = {"sig": None, "generating": False}
_rca_lock = threading.Lock()


def _signature(prediction: dict) -> str:
    ft = str(prediction.get("predicted_failure_type") or "unknown").strip().lower()
    return "".join(c if c.isalnum() else "-" for c in ft).strip("-") or "unknown"


def _reset_rca_dedupe() -> None:
    """Forget the last incident signature so a re-injected incident regenerates a
    fresh RCA. The RCA history STACK is kept (past analyses stay for reference);
    only a full stack wipe removes it."""
    with _rca_lock:
        _rca_state["sig"] = None


def _append_rca(rca: dict) -> None:
    """Prepend a new RCA onto the bounded history stack (newest first)."""
    hist: list = []
    if _RCA_HISTORY.exists():
        try:
            loaded = json.loads(_RCA_HISTORY.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                hist = loaded
        except (OSError, json.JSONDecodeError):
            hist = []
    hist.insert(0, rca)
    try:
        _RCA_HISTORY.write_text(json.dumps(hist[:12], indent=2, default=str),
                                encoding="utf-8")
    except OSError:
        pass


def _recover_collector(collector) -> None:
    """Keep only the healthy baseline (drop the incident spikes) so the pipeline
    settles GREEN at once and re-detection of the NEXT incident stays fast."""
    healthy = [h for h in collector.history if (
        (h.get("latency_ms") or 0) < config.SLA_LATENCY_MS
        and (h.get("null_rate") or 0) < 0.1
        and (h.get("dup_rate") or 0) < 0.1)]
    collector.history.clear()
    for h in healthy[-30:]:
        collector.history.append(h)


def _maybe_generate_rca(prediction: dict, signals_window: list, brain, tick: int) -> None:
    """Kick off a one-shot background RCA for a NEW incident signature."""
    sig = _signature(prediction)
    with _rca_lock:
        if _rca_state["generating"] or _rca_state["sig"] == sig:
            return
        _rca_state["generating"] = True

    def _work() -> None:
        try:
            audit_trail.stream_emit("guardian_rca_started", tick=tick, signature=sig)
            r = rca_mod.generate_rca(prediction, signals_window, brain)
            r["signature"] = sig
            r["generated_tick"] = tick
            r["generated_at"] = datetime.now(timezone.utc).isoformat()
            r["level"] = "RED" if (prediction.get("risk_score") or 0) >= config.RISK_RED else "AMBER"
            _append_rca(r)
            with _rca_lock:
                _rca_state["sig"] = sig
            audit_trail.audit("rca_generated", signature=sig,
                              source=r.get("source"), title=r.get("title"))
            print(f"    >>> RCA ready ({r.get('source')}): {r.get('title')}", flush=True)
        except Exception as exc:  # noqa: BLE001 - RCA must never kill the engine
            print(f"    [guardian] RCA generation error: {exc}", flush=True)
        finally:
            with _rca_lock:
                _rca_state["generating"] = False

    threading.Thread(target=_work, daemon=True).start()


def _apply_merged_fix(pr: dict) -> bool:
    """The human merged the gated code-fix PR on GitHub — pull the merged source
    into the running pipeline and hot-reload it, so the LIVE engine starts using
    the fix immediately. Returns True when the running code actually changed."""
    import importlib
    merged = github_gov.fetch_main_source(github_gov.SOURCE_PATH)
    if not merged:
        return False
    try:
        current = _SOURCE_FILE.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if merged.strip() == current.strip():
        return False
    try:
        _SOURCE_FILE.write_text(merged, encoding="utf-8")
        importlib.reload(etl_mod)
    except Exception:  # noqa: BLE001 - a bad merge must never kill the loop
        return False
    audit_trail.stream_emit("guardian_fix_merged", pr=pr.get("url"),
                            number=pr.get("number"), file=github_gov.SOURCE_PATH)
    print(f"    >>> MERGED FIX PULLED from {pr.get('url')} — "
          f"{github_gov.SOURCE_PATH} hot-reloaded into the live pipeline.",
          flush=True)
    return True


def _consume_pending() -> dict | None:
    """One-shot read+delete of ``data/pending_incident.json``.

    The dashboard writes this file when an audience member injects their own
    incident, or the sentinel ``{"reset": true}`` when they press *Clear*. Both
    are handled here so the live engine reacts the instant a button is clicked.
    """
    if not _PENDING.exists():
        return None
    try:
        spec = json.loads(_PENDING.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        spec = None
    try:
        _PENDING.unlink()
    except OSError:
        pass
    if isinstance(spec, dict) and (spec.get("ops") or spec.get("reset")
                                   or spec.get("apply_fix")):
        return spec
    return None


def main() -> None:
    collector = SignalCollector()
    agent = PredictiveAgent()

    brain = (
        f"{agent.llm.name} ({agent.llm.model})"
        if agent.llm.available
        else "grounded heuristic (headless container — the transparent fallback brain)"
    )
    print(f"[guardian] REAL-TIME engine online. interval={INTERVAL}s "
          f"products={config.PRODUCTS}", flush=True)
    print(f"[guardian] AI brain: {brain}", flush=True)
    print("[guardian] inject your own incident live from the dashboard "
          f"(http://localhost:{config.DASHBOARD_PORT}) — it ramps from the click.",
          flush=True)

    active_mode = START_INJECT
    active_spec: dict | None = None
    active_inject_at = START_INJECT_AT
    recovery_left = 0
    active_fix_label: str | None = None
    fix_tick = 0
    tick = 0
    # PRs already merged BEFORE this process started are history, not events.
    try:
        seen_merged = {p["number"] for p in github_gov.merged_guardian_prs()}
    except Exception:  # noqa: BLE001
        seen_merged = set()

    while True:
        tick += 1
        try:
            # 0) did the human MERGE the gated fix PR on GitHub? If so, pull the
            #    merged source into the running pipeline and heal automatically.
            if tick % MERGE_POLL_TICKS == 0:
                try:
                    for pr in github_gov.merged_guardian_prs():
                        if pr["number"] in seen_merged:
                            continue
                        seen_merged.add(pr["number"])
                        if _apply_merged_fix(pr) and active_mode != "none":
                            recovery_left = RECOVERY_TICKS
                            fix_tick = tick
                            active_fix_label = f"merged PR #{pr['number']}"
                            audit_trail.stream_emit("guardian_fix_applying",
                                                    tick=tick, fix=active_fix_label)
                            print(f"    >>> AUTO-HEAL from {active_fix_label} — "
                                  f"pipeline recovering over {RECOVERY_TICKS} ticks...",
                                  flush=True)
                        break
                except Exception:  # noqa: BLE001 - watcher must never break the loop
                    pass

            # 1) audience-authored incident / reset arriving from the dashboard
            pend = _consume_pending()
            if pend is not None:
                if pend.get("reset"):
                    active_mode, active_spec = "none", None
                    recovery_left, active_fix_label = 0, None
                    _recover_collector(collector)
                    clear_incident()
                    _reset_rca_dedupe()
                    audit_trail.stream_emit("guardian_incident_reset", tick=tick)
                    print(f"    >>> RESET at tick {tick} — fault cleared, "
                          "pipeline returns to healthy live data.", flush=True)
                elif pend.get("apply_fix"):
                    # Operator performed the AI's recommended remediation. Decay the
                    # fault to 0 over RECOVERY_TICKS so they WATCH the pipeline heal.
                    if active_mode != "none":
                        recovery_left = RECOVERY_TICKS
                        fix_tick = tick
                        active_fix_label = str(pend.get("label") or "recommended fix")[:80]
                        audit_trail.stream_emit("guardian_fix_applying", tick=tick,
                                                fix=active_fix_label)
                        print(f"    >>> APPLY FIX: {active_fix_label} — pipeline "
                              f"recovering over {RECOVERY_TICKS} ticks...", flush=True)
                else:
                    active_mode = "custom"
                    active_spec = pend
                    active_inject_at = tick
                    recovery_left, active_fix_label = 0, None
                    audit_trail.stream_emit(
                        "guardian_incident_injected", tick=tick,
                        label=str(pend.get("label", "custom incident"))[:80],
                        ops=len(pend.get("ops") or []),
                    )
                    print(f"    >>> LIVE INJECT: {pend.get('label', 'custom incident')} "
                          f"— ramp starts now at tick {tick}", flush=True)

            # Recovery ramp: an applied fix decays the fault toward 0 over a few
            # ticks so the operator sees the pipeline heal, then it stands down.
            rf = 1.0
            eff_tick = tick
            if recovery_left > 0:
                recovery_left -= 1
                rf = recovery_left / float(RECOVERY_TICKS)
                eff_tick = fix_tick          # freeze ramp at its peak; rf decays it
                if recovery_left == 0:
                    active_mode, active_spec = "none", None
                    _recover_collector(collector)
                    clear_incident()
                    _reset_rca_dedupe()
                    audit_trail.stream_emit("guardian_fix_recovered", tick=tick,
                                            fix=active_fix_label)
                    print(f"    >>> RECOVERED via '{active_fix_label}' — pipeline healthy.",
                          flush=True)
                    active_fix_label = None

            # 2) EXTRACT live trades + apply the ramping fault
            raw = fetch_batch()
            raw = apply_fault(raw, active_mode, eff_tick,
                              inject_at=active_inject_at, spec=active_spec, recovery=rf)

            # 3) TRANSFORM + LOAD (+ modeled load-latency / hard timeout break)
            t0 = time.time()
            etl = etl_mod.run_etl(raw)
            latency_ms = (time.time() - t0) * 1000.0
            latency_ms, load_error = load_latency(
                active_mode, eff_tick, latency_ms,
                inject_at=active_inject_at, spec=active_spec, recovery=rf,
            )
            if load_error and not etl["failed"]:
                etl["failed"] = True
                etl["error"] = load_error

            # 4) SIGNALS (kept in memory -> trends drive early prediction)
            signals = collector.collect(raw, etl, latency_ms)
            _persist_history(collector)

            audit_trail.audit(
                "etl_run", record_count=etl["record_count"],
                null_rate=etl["null_rate"], revenue=etl["aggregate"]["total_revenue"],
                latency_ms=round(latency_ms, 1), failed=etl["failed"], error=etl["error"],
            )
            audit_trail.stream_emit(
                "guardian_tick", tick=tick, mode=active_mode,
                records=etl["record_count"], null_rate=round(etl["null_rate"], 3),
                latency_ms=round(latency_ms, 1), revenue=etl["aggregate"]["total_revenue"],
            )

            # 5) PREDICT + GOVERN. The real-time risk uses the fast grounded
            # heuristic; the detailed Opus RCA is generated once per incident in
            # a background thread so this loop never blocks on a model call.
            prediction = agent.predict(collector, INTERVAL, use_llm=False)
            decision = decide(prediction)
            emit(tick, prediction, decision)
            if decision["level"] == "GREEN":
                clear_incident()
            else:
                _maybe_generate_rca(prediction, list(collector.history),
                                    agent.llm, tick)

            if etl["failed"]:
                print(f"    !!! ETL BROKE: {etl['error']}", flush=True)
                audit_trail.audit(
                    "etl_failed_after_prediction", level="RED", error=etl["error"],
                    predicted_risk=prediction.get("risk_score"),
                    predicted_type=prediction.get("predicted_failure_type"),
                )
                agent.learn(signals, prediction, outcome="failed")
                # Keep the incident ACTIVE (RED) until the operator clicks Clear, so
                # the failure, its RCA and the governed issue/PR stay on screen for
                # as long as needed. Clear (or a real remediation) stands it down.
            else:
                agent.learn(signals, prediction, outcome="ok")
        except Exception as exc:  # noqa: BLE001 - a tick hiccup must never kill the engine
            print(f"    [guardian] tick {tick} error: {exc}", flush=True)

        time.sleep(max(0.5, INTERVAL))


def _persist_history(collector: SignalCollector) -> None:
    """Write the rolling in-memory signal window so the dashboard + chat show the
    live signals (they read ``signal_history.json``)."""
    try:
        config.SIGNAL_HISTORY_FILE.write_text(
            json.dumps(list(collector.history)[-40:], indent=2, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass


if __name__ == "__main__":
    main()
