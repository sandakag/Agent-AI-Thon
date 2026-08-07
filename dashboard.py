"""Predictive Pipeline Guardian — live monitoring dashboard (dependency-free).

A single-file, pure-standard-library web app that gives one pane of glass over
the guardian:

    * A red / amber / green banner driven by the latest prediction
    * Headline KPIs — risk, pipeline health, predicted lead time, confidence
    * The prediction stream — every forecast, its risk and the brain that made it
    * Governance — the predicted-incident issues and gated preventive PRs opened
    * Live revenue per product (the ETL warehouse)
    * Audit integrity — verifies the tamper-evident hash-chain

It also exposes a Prometheus ``/metrics`` endpoint (scraped by Prometheus,
visualised in Grafana). It reads the same hash-chained ``audit/audit.jsonl``
every other component writes to — no database wiring, no extra packages.

Run:
    python -m dashboard                    # serves http://localhost:8089
    DASHBOARD_PORT=9000 python -m dashboard
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from agent import audit_trail
from agent.brain import make_brain, BrainError
from alerting import notifier


def build_summary() -> dict:
    events = audit_trail.load_events()
    predictions = [e for e in events if e.get("event") == "prediction"]
    issues = [e for e in events if e.get("event") == "governance_issue_opened"]
    prs = [e for e in events if e.get("event") == "governance_pr_opened"]
    etl_fail = [e for e in events
                if (e.get("event") == "etl_run" and e.get("failed")) or
                e.get("event") == "etl_failed_after_prediction"]
    warnings = [p for p in predictions if p.get("level") in ("AMBER", "RED")]

    last = predictions[-1] if predictions else {}
    intact, broken_at = audit_trail.verify_chain(events)

    revenue = 0.0
    if config.WAREHOUSE_FILE.exists():
        try:
            wh = json.loads(config.WAREHOUSE_FILE.read_text())
            revenue = round(sum(float(v) for v in wh.values()), 2)
        except (json.JSONDecodeError, ValueError, TypeError):
            revenue = 0.0

    banner = {"level": "GREEN"}
    if config.INCIDENTS_FILE.exists():
        try:
            banner = json.loads(config.INCIDENTS_FILE.read_text())
        except json.JSONDecodeError:
            banner = {"level": "GREEN"}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "banner": banner,
        "last": last,
        "totals": {
            "predictions": len(predictions),
            "warnings": len(warnings),
            "issues": len(issues),
            "prs": len(prs),
            "etl_failures": len(etl_fail),
            "events": len(events),
            "revenue": revenue,
        },
        "audit": {"intact": intact, "broken_at": broken_at, "records": len(events)},
        "recent_predictions": list(reversed(predictions[-14:])),
        "recent_issues": list(reversed(issues[-6:])),
        "recent_prs": list(reversed(prs[-6:])),
        "event_counts": dict(Counter(e.get("event") for e in events).most_common(12)),
    }


# ---------------------------------------------------------------------------
# Metric helpers (AI / pipeline / stream throughput series)
# ---------------------------------------------------------------------------
def _last_event(events: list, name: str) -> dict:
    for e in reversed(events):
        if e.get("event") == name:
            return e
    return {}


def _stream_totals() -> dict:
    """Aggregate the always-on live heartbeat (audit/stream.jsonl) into pipeline
    throughput counters for the ETL performance dashboard."""
    extract = load = cycles = 0
    last_revenue = 0.0
    if config.STREAM_LOG.exists():
        try:
            for line in config.STREAM_LOG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = rec.get("event")
                if ev == "stream_extract_ok":
                    extract += int(rec.get("count", 0) or 0)
                    cycles += 1
                elif ev == "stream_load_ok":
                    load += int(rec.get("published", 0) or 0)
                elif ev == "stream_transform_ok":
                    try:
                        last_revenue = float(rec.get("revenue", last_revenue) or last_revenue)
                    except (TypeError, ValueError):
                        pass
        except OSError:
            pass
    return {"extract": extract, "load": load, "cycles": cycles, "last_revenue": last_revenue}


def _record_grafana_alert(payload: dict) -> None:
    """Grafana fires an alert rule -> its webhook contact point POSTs here -> we
    log it to the live stream (Promtail -> Loki), so the raised Grafana alert is
    also visible in the real-time log panels. Closes the observability loop."""
    status = str(payload.get("status", "firing"))
    alerts = payload.get("alerts") or [{}]
    for a in alerts:
        labels = a.get("labels", {}) if isinstance(a, dict) else {}
        anns = a.get("annotations", {}) if isinstance(a, dict) else {}
        audit_trail.stream_emit(
            "grafana_alert",
            level="RED" if str(a.get("status", status)) == "firing" else "GREEN",
            status=a.get("status", status),
            alertname=labels.get("alertname", "guardian"),
            severity=labels.get("severity", ""),
            summary=anns.get("summary") or anns.get("description", ""),
        )


# ---------------------------------------------------------------------------
# Chat box + open-ended incident injection (audience self-service)
# ---------------------------------------------------------------------------
CHAT_LOG = config.DATA_DIR / "chat_log.json"
PENDING_INCIDENT = config.DATA_DIR / "pending_incident.json"

_CHAT_SYSTEM = (
    "You are the Guardian — a friendly, expert AI SRE assistant for a Predictive "
    "Pipeline Guardian that forecasts data-pipeline failures BEFORE they happen. "
    "Answer conversationally and correctly. For questions about the LIVE pipeline, "
    "ground your answer ONLY in the telemetry provided (risk, SLA, latency, data "
    "quality, and the current/last root-cause analysis) and never invent numbers. "
    "For general or conceptual questions — e.g. what a term or acronym means — just "
    "answer normally (for example, RCA stands for Root Cause Analysis). Keep replies "
    "concise (2-4 sentences) unless the operator asks you to go deep. If the telemetry "
    "doesn't support an answer, say so plainly."
)

_brain = None


def _get_brain():
    global _brain
    if _brain is None:
        try:
            _brain = make_brain()
        except Exception:  # noqa: BLE001
            _brain = False  # sentinel: tried and failed
    return _brain or None


def _chat_load() -> list:
    if CHAT_LOG.exists():
        try:
            return json.loads(CHAT_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _chat_save(turns: list) -> None:
    try:
        CHAT_LOG.write_text(json.dumps(turns[-20:], indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def _signal_history(n: int = 12) -> list:
    """The recent rolling signal window the live engine persists."""
    if config.SIGNAL_HISTORY_FILE.exists():
        try:
            hist = json.loads(config.SIGNAL_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(hist, list):
                return hist[-n:]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _series(hist: list, field: str) -> list:
    return [h[field] for h in hist
            if isinstance(h, dict) and isinstance(h.get(field), (int, float))]


def _slope(series: list) -> float:
    """Least-squares slope of a short series vs its index (0 if < 3 points)."""
    n = len(series)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(series) / n
    denom = sum((x - mx) ** 2 for x in xs) or 1e-9
    return sum((xs[i] - mx) * (series[i] - my) for i in range(n)) / denom


def _sla_view(hist: list) -> dict:
    """Turn the live latency window into a plain-English SLA verdict + ETA."""
    lat = _series(hist, "latency_ms")
    cur = lat[-1] if lat else None
    soft = config.SLA_LATENCY_MS
    hard = config.LATENCY_TIMEOUT_MS
    slope = _slope(lat[-8:]) if len(lat) >= 3 else 0.0
    eta = None
    verdict = "unknown"
    if cur is not None:
        if cur >= hard:
            verdict = "BREACHING (past the hard timeout)"
        elif cur >= soft:
            verdict = "AT RISK (over the soft SLA)"
        elif slope > 1 and cur > soft * 0.4:
            verdict = "approaching the SLA"
        else:
            verdict = "within SLA"
        if slope > 1e-6 and cur < hard:
            eta = round((hard - cur) / slope, 1)
    return {
        "current_ms": round(cur, 1) if cur is not None else None,
        "soft_sla_ms": soft,
        "hard_ceiling_ms": hard,
        "trend_ms_per_tick": round(slope, 1),
        "verdict": verdict,
        "ticks_to_breach": eta,
    }


def _sparkline(series: list, w: int = 260, h: int = 46, color: str = "#58a6ff",
               thresholds=None, vmin=None, vmax=None) -> str:
    """A tiny dependency-free inline-SVG sparkline (no JS, no libraries).

    ``thresholds`` is a list of ``(value, color)`` dashed reference lines — used
    to draw the SLA / hard-timeout lines on the latency trend so a breach is
    obvious at a glance.
    """
    pts = [float(x) for x in series if isinstance(x, (int, float))][-40:]
    if len(pts) < 2:
        return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
                f'<text x="6" y="{h // 2 + 4}" fill="#6e7681" font-size="11">'
                'warming up…</text></svg>')
    lo = vmin if vmin is not None else min(pts)
    hi = vmax if vmax is not None else max(pts)
    hi = max(hi, max(pts))
    lo = min(lo, min(pts))
    rng = (hi - lo) or 1.0
    n = len(pts)

    def xy(i: int, v: float):
        x = i * (w - 4) / (n - 1) + 2
        y = h - 3 - ((v - lo) / rng) * (h - 8)
        return x, y

    coords = [xy(i, v) for i, v in enumerate(pts)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    tl = ""
    for tval, tcol in (thresholds or []):
        if lo <= tval <= hi:
            _, ty = xy(0, tval)
            tl += (f'<line x1="0" y1="{ty:.1f}" x2="{w}" y2="{ty:.1f}" stroke="{tcol}" '
                   f'stroke-width="1" stroke-dasharray="3,3" opacity="0.75"/>')
    lx, ly = coords[-1]
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            'preserveAspectRatio="none">'
            f'{tl}<polyline fill="none" stroke="{color}" stroke-width="2" '
            f'points="{poly}"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.6" fill="{color}"/></svg>')


def _sla_color(verdict: str) -> str:
    v = (verdict or "").lower()
    if "breach" in v:
        return "#f85149"
    if "at risk" in v or "approach" in v:
        return "#d98c00"
    if "within" in v:
        return "#3fb950"
    return "#8b949e"


def _render_realtime(sla: dict, lat_series: list, risk_series: list,
                     rec_series: list, hist: list) -> str:
    """The real-time monitoring strip: latency-vs-SLA, predicted-risk and
    throughput/quality, each with a live sparkline — so an operator sees the
    pipeline breathing and a breach coming, not just static counters."""
    soft = sla.get("soft_sla_ms") or 4000
    hard = sla.get("hard_ceiling_ms") or 9000
    cur = sla.get("current_ms")
    verdict = sla.get("verdict", "unknown")
    scol = _sla_color(verdict)
    eta = sla.get("ticks_to_breach")
    trend = sla.get("trend_ms_per_tick", 0.0)
    lat_vals = [x for x in lat_series if isinstance(x, (int, float))]
    lat_max = max([hard * 1.05] + lat_vals) if lat_vals else hard * 1.05
    lat_spark = _sparkline(lat_series, color=scol,
                           thresholds=[(soft, "#d98c00"), (hard, "#f85149")],
                           vmin=0, vmax=lat_max)
    cur_txt = f"{cur:.0f} ms" if cur is not None else "– ms"
    if trend > 1 and eta is not None and (cur is None or cur < hard):
        eta_txt = f" · ~{eta:.0f} ticks to breach"
    elif trend > 1:
        eta_txt = " · climbing"
    elif trend < -1:
        eta_txt = " · easing"
    else:
        eta_txt = ""

    latest_risk = risk_series[-1] if risk_series else None
    rv = latest_risk or 0
    rlevel = "RED" if rv >= config.RISK_RED else ("AMBER" if rv >= config.RISK_AMBER else "GREEN")
    rcol = _COLOR.get(rlevel, "#1f9d55")
    risk_spark = _sparkline(risk_series, color=rcol,
                            thresholds=[(config.RISK_AMBER, "#d98c00"),
                                        (config.RISK_RED, "#f85149")],
                            vmin=0, vmax=100)

    latest_sig = hist[-1] if hist else {}
    rc = latest_sig.get("record_count")
    nr = latest_sig.get("null_rate")
    drift = latest_sig.get("schema_drift")
    rec_spark = _sparkline(rec_series, color="#58a6ff", vmin=0)
    drift_badge = ('<span class="pill" style="background:#c0392b">SCHEMA DRIFT</span>'
                   if drift else '<span class="pill" style="background:#1f9d55">no drift</span>')
    nr_txt = f"{nr * 100:.0f}%" if isinstance(nr, (int, float)) else "–"
    rc_txt = rc if rc is not None else "–"

    return (
        '<div class="sec">📈 Live SLA &amp; signal trends (auto-refresh ~5s)</div>'
        '<div class="grid3">'
        f'<div class="panel sp"><div class="l">Processing latency vs SLA</div>'
        f'<div class="v" style="color:{scol}">{cur_txt}</div>'
        f'<div class="s">soft {soft:.0f}ms · hard {hard:.0f}ms · '
        f'<b style="color:{scol}">{_esc(verdict)}</b>{eta_txt}</div>'
        f'<div class="spark">{lat_spark}</div>'
        '<div class="s muted">orange = soft SLA · red = hard timeout ceiling</div></div>'
        f'<div class="panel sp"><div class="l">Predicted risk (0–100)</div>'
        f'<div class="v" style="color:{rcol}">{latest_risk if latest_risk is not None else "–"}</div>'
        f'<div class="s">amber ≥ {config.RISK_AMBER:.0f} · red ≥ {config.RISK_RED:.0f} — '
        'the agent fires BEFORE the break</div>'
        f'<div class="spark">{risk_spark}</div></div>'
        f'<div class="panel sp"><div class="l">Throughput &amp; data quality</div>'
        f'<div class="v">{rc_txt} <span style="font-size:13px;color:#8b949e">records/run</span></div>'
        f'<div class="s">null-rate {nr_txt} · {drift_badge}</div>'
        f'<div class="spark">{rec_spark}</div></div>'
        '</div>'
    )


def _load_rca_history() -> list:
    """The stack of RCAs the live engine has produced (newest first)."""
    f = config.DATA_DIR / "rca_history.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, list):
                return [r for r in d if isinstance(r, dict) and r.get("root_cause")]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _rca_entry_html(rca: dict, open_: bool = False) -> str:
    """One collapsible RCA card for the stack."""
    def lis(items) -> str:
        return "".join(f"<li>{_esc(i)}</li>" for i in (items or [])) \
            or '<li class="muted">(none)</li>'
    lvl = str(rca.get("level") or "AMBER").upper()
    col = _COLOR.get(lvl, "#d98c00")
    src = str(rca.get("source") or "")
    badge = ("Opus 4.8" if "opus" in src.lower()
             else "grounded" if "heuristic" in src.lower() else _esc(src))
    when = _esc((rca.get("generated_at") or "")[11:19])
    analysis = _esc(rca.get("detailed_analysis", "")).replace("\n", "<br>")
    detection = _esc(rca.get("detection_method", "")).replace("\n", "<br>")
    fix = str(rca.get("fix_type") or "").lower()
    fix_line = ("🛠 <b>Code/config fix needed</b> — a gated PR is staged for review when you approve."
                if fix == "code" else
                "🛠 <b>Operational (manual) fix</b> — run the steps above; no code change needed."
                if fix == "manual" else "")
    return (
        f'<details class="rcaitem"{" open" if open_ else ""}>'
        f'<summary><span class="pill" style="background:{col}">{_esc(lvl)}</span> '
        f'{_esc(rca.get("title", "Predicted incident"))} '
        f'<span class="muted">· {when} UTC · {badge}</span></summary>'
        '<div class="rcabody">'
        f'<div class="s"><b>What is the issue:</b> {_esc(rca.get("summary", ""))}</div>'
        f'<div style="margin-top:6px"><b>Root cause:</b> {_esc(rca.get("root_cause", ""))}</div>'
        + (f'<div style="margin-top:6px"><b>🔎 How the AI detected it early</b>'
           f'<div class="a">{detection}</div></div>' if detection else '')
        + f'<div style="margin-top:6px"><b>Detailed analysis</b><div class="a">{analysis}</div></div>'
        '<div class="rcagrid">'
        f'<div><b>Timeline</b><ol>{lis(rca.get("timeline"))}</ol></div>'
        f'<div><b>✅ Do these steps NOW</b><ul>{lis(rca.get("immediate_actions"))}</ul></div>'
        f'<div><b>Preventive measures</b><ul>{lis(rca.get("preventive_measures"))}</ul></div>'
        f'<div><b>Impact if not acted on</b><div class="a">{_esc(rca.get("impact", ""))}</div>'
        f'<b>Evidence</b><ul>{lis(rca.get("evidence"))}</ul></div>'
        '</div>'
        + (f'<div class="s" style="margin-top:8px">{fix_line}</div>' if fix_line else '')
        + f'<div class="muted" style="margin-top:6px">confidence {_esc(rca.get("confidence", ""))}</div>'
        '</div></details>'
    )


def _render_rca_html(banner_level: str) -> str:
    """Render the RCA STACK. When an incident is ACTIVE (banner AMBER/RED) the
    newest analysis is expanded and flagged ACTIVE; older ones collapse into a
    stack. When GREEN, nothing shows as active — past analyses live in a collapsed
    'recent incidents' stack, so a healthy board is never confused with a live one."""
    history = _load_rca_history()
    if not history:
        return ""
    active = str(banner_level).upper() in ("AMBER", "RED")
    out = ['<div class="sec">🔬 AI Root-Cause Analysis</div>']
    if active:
        out.append('<div class="rcabadge red">● ACTIVE INCIDENT — analysis below</div>')
        out.append(_rca_entry_html(history[0], open_=True))
        rest = history[1:]
        if rest:
            out.append('<details class="rcastack"><summary>▸ Earlier incident analyses '
                       f'({len(rest)})</summary>')
            out.extend(_rca_entry_html(r) for r in rest)
            out.append('</details>')
    else:
        out.append('<div class="panel rcaok">✓ No active incident — the pipeline is healthy. '
                   'Past analyses are stacked below for reference.</div>')
        out.append('<details class="rcastack"><summary>▸ Recent incident analyses '
                   f'({len(history)})</summary>')
        out.extend(_rca_entry_html(r) for r in history)
        out.append('</details>')
    return "".join(out)


def _chat_context() -> dict:
    s = build_summary()
    hist = _signal_history(12)
    signals = hist[-1] if hist else {}
    last = s.get("last") or {}
    return {
        "banner_level": (s.get("banner") or {}).get("level"),
        "latest_prediction": {
            "risk_score": last.get("risk_score"),
            "pipeline_health": last.get("pipeline_health"),
            "predicted_failure_type": last.get("predicted_failure_type"),
            "lead_time_minutes": last.get("lead_time_minutes"),
            "confidence": last.get("confidence"),
            "evidence": last.get("evidence"),
            "recommended_action": last.get("recommended_action"),
            "brain": last.get("source"),
        },
        "latest_signals": signals,
        "sla": _sla_view(hist),
        "root_cause_analysis": (_load_rca_history()[:1] or [None])[0],
        "recent_failure_types": [p.get("predicted_failure_type")
                                 for p in s.get("recent_predictions", [])][:8],
        "totals": s.get("totals"),
        "audit_intact": (s.get("audit") or {}).get("intact"),
    }


# --- Remediation playbook (used by the grounded assistant + shown in answers) --
def _remediation_for(failure_type: str) -> str:
    ft = (failure_type or "").lower()
    if "schema" in ft:
        return ("resolve the renamed/aliased field and quarantine the bad records "
                "before load, so the parser stops dropping rows.")
    if "latency" in ft or "timeout" in ft or "load" in ft:
        return _latency_remediation()
    if "null" in ft or "quality" in ft:
        return ("quarantine + repair the malformed records and resolve field aliases "
                "before load — the batch is trending toward the null-rate line that "
                "zeroes revenue.")
    if "stall" in ft or "throughput" in ft or "volume" in ft:
        return ("check the upstream producer / consumer lag, scale consumers and "
                "backfill the window before the batch starves.")
    if "anomaly" in ft or "source" in ft or "outage" in ft:
        return ("investigate the flagged signal against recent upstream / deploy "
                "changes, confirm the deviation is real, then contain the source "
                "before it breaches.")
    return "keep monitoring; no action is needed yet."


def _latency_remediation() -> str:
    return ("scale out the ETL consumers and raise parallelism, add backpressure, "
            "and chunk the batch so processing time stays under the SLA before the "
            "load-timeout aborts the pipeline.")


def _full_status(ctx: dict) -> str:
    p = ctx.get("latest_prediction") or {}
    sig = ctx.get("latest_signals") or {}
    sla = ctx.get("sla") or {}
    lvl = (ctx.get("banner_level") or "GREEN").upper()
    ft = p.get("predicted_failure_type")
    bits = [f"Status {lvl} · risk {p.get('risk_score')}/100"]
    if sla.get("current_ms") is not None:
        bits.append(f"latency {sla['current_ms']:.0f}ms/{sla['soft_sla_ms']:.0f} SLA")
    if isinstance(sig.get("null_rate"), (int, float)):
        bits.append(f"null {sig['null_rate'] * 100:.0f}%")
    if sig.get("record_count") is not None:
        bits.append(f"{sig['record_count']} records")
    line = " · ".join(bits) + "."
    if ft and ft != "none":
        line += (f" Predicting {ft} ~{p.get('lead_time_minutes')} min out — "
                 f"{p.get('recommended_action') or _remediation_for(ft)}")
    else:
        line += " No failure predicted; every signal is inside its normal band."
    return line


_GREETINGS = ("hi", "hello", "hey", "yo", "hola", "howdy", "hiya", "sup", "gm",
              "good morning", "good afternoon", "good evening", "namaste")


def _grounded_answer(q: str, ctx: dict) -> str:
    """A genuinely useful, conversational answer grounded ONLY in live telemetry.

    Runs whenever the LLM brain isn't reachable (e.g. a headless container). It
    reads the operator's actual question, routes it to the right slice of the
    telemetry, and answers concretely — so "hello", "what will break?", and
    "are we over SLA?" each get a distinct, grounded reply.
    """
    ql = " ".join(q.lower().split())
    p = ctx.get("latest_prediction") or {}
    sig = ctx.get("latest_signals") or {}
    sla = ctx.get("sla") or {}
    lvl = (ctx.get("banner_level") or "GREEN").upper()
    ft = p.get("predicted_failure_type")
    has_pred = bool(ft and ft != "none")
    risk = p.get("risk_score")
    lead = p.get("lead_time_minutes")

    def has(*words) -> bool:
        return any(w in ql for w in words)

    # greetings / thanks -------------------------------------------------------
    if ql in ("thanks", "thank you", "ty", "thx"):
        return "Anytime! " + _full_status(ctx) + " Ask me what's most likely to break, when, or how to prevent it."
    if any(ql == g or ql.startswith(g + " ") or ql.startswith(g + "!") for g in _GREETINGS):
        return ("Hey — I'm the Predictive Pipeline Guardian. " + _full_status(ctx) +
                " You can ask me “what's the biggest risk right now and when will it break?”, "
                "“are we breaching the latency SLA?”, or “what should the on-call do?”")

    # help ---------------------------------------------------------------------
    if has("what can you", "how do you work", "what do you do") or ql in ("help", "?", "commands"):
        return ("I forecast data-pipeline failures BEFORE they happen from the live signals. Try:\n"
                f"• what's the biggest risk right now and when will it break?\n"
                f"• are we within our latency SLA? (soft {sla.get('soft_sla_ms', 4000):.0f}ms / hard {sla.get('hard_ceiling_ms', 9000):.0f}ms)\n"
                f"• what should the on-call do to prevent it?\n"
                f"• how's data quality / throughput / revenue?\n"
                "Then inject an incident below and watch me catch it early.")

    # SLA / latency ------------------------------------------------------------
    if has("sla", "latency", "slow", "response time", "timeout", "how fast", "lag"):
        cur = sla.get("current_ms")
        if cur is None:
            return "No latency reading yet — give the live engine a few seconds to warm up, then ask again."
        soft = sla.get("soft_sla_ms"); hard = sla.get("hard_ceiling_ms")
        trend = sla.get("trend_ms_per_tick", 0.0); eta = sla.get("ticks_to_breach")
        parts = [f"Processing latency is **{cur:.0f} ms** vs a {soft:.0f} ms soft SLA and a "
                 f"{hard:.0f} ms hard timeout — **{sla.get('verdict', 'unknown')}**."]
        if trend > 1:
            parts.append(f"It's climbing ~{trend:.0f} ms/tick.")
            if eta is not None and cur < hard:
                parts.append(f"At this rate it hits the {hard:.0f} ms ceiling in ~{eta:.0f} ticks — "
                             "the point the load stage aborts the batch.")
            parts.append("Prevention: " + _latency_remediation())
        elif trend < -1:
            parts.append(f"It's easing (~{abs(trend):.0f} ms/tick) — recovering toward normal.")
        else:
            parts.append("It's stable.")
        return " ".join(parts)

    # deep explanation / root-cause analysis ----------------------------------
    if has("rca", "root cause", "root-cause", "explain", "deep dive", "detailed",
           "analysis", "analyse", "analyze", "walk me through", "break it down"):
        rca = ctx.get("root_cause_analysis")
        if rca and rca.get("detailed_analysis"):
            parts = [f"**{rca.get('title', 'Root-cause analysis')}** — {rca.get('summary', '')}",
                     f"**Root cause:** {rca.get('root_cause', '')}",
                     rca.get("detailed_analysis", "")]
            im = rca.get("immediate_actions") or []
            pv = rca.get("preventive_measures") or []
            if im:
                parts.append("**Immediate actions:** " + "; ".join(str(x) for x in im))
            if pv:
                parts.append("**Prevention:** " + "; ".join(str(x) for x in pv))
            return "\n\n".join(str(p) for p in parts if p)
        if not has_pred and lvl == "GREEN":
            return ("No incident is active, so there's no root-cause analysis yet. The "
                    "pipeline is GREEN and every signal is inside its normal band. Inject "
                    "an incident and I'll produce a full RCA (Opus 4.8) — root cause, "
                    "timeline, impact and prevention.")

    # remediation / action -----------------------------------------------------
    if has("what should", "what do i do", "how to prevent", "how do i fix", "remediat",
           "measures", "mitigat", "recommend", "prevent", "avoid", "action", "steps"):
        if not has_pred and lvl == "GREEN":
            return (_full_status(ctx) + " No action needed yet — keep monitoring. "
                    "Inject an incident below to see the exact remediation I'd hand the on-call.")
        why = "; ".join(str(e) for e in (p.get("evidence") or [])[:3])
        return (f"Predicted **{ft}** at risk {risk}/100 (~{lead} min lead). "
                + (f"Why: {why}. " if why else "")
                + "Recommended action: " + (p.get("recommended_action") or _remediation_for(ft)))

    # biggest risk / when will it break ---------------------------------------
    if has("risk", "break", "fail", "when", "worst", "biggest", "danger", "incident",
           "predict", "happen", "wrong"):
        if not has_pred and lvl == "GREEN":
            return (f"No failure is predicted right now — the pipeline is GREEN (risk {risk}/100) and every "
                    "signal is inside its normal band. The instant one starts drifting I'll tell you exactly "
                    "what will break, when, and how to stop it.")
        why = "; ".join(str(e) for e in (p.get("evidence") or [])[:3])
        conf = p.get("confidence")
        return (f"Biggest risk: **{ft}** — risk {risk}/100"
                + (f", confidence {conf}." if conf is not None else ".")
                + (f" ~{lead} min of lead time before it would break." if lead else "")
                + (f" Signals driving it: {why}." if why else "")
                + " To prevent it: " + (p.get("recommended_action") or _remediation_for(ft)))

    # data quality / null ------------------------------------------------------
    if has("null", "data quality", "missing", "quality", "empty", "corrupt"):
        nr = sig.get("null_rate")
        if not isinstance(nr, (int, float)):
            return "No data-quality reading yet."
        crit = config.NULL_RATE_CRITICAL
        v = "critical" if nr >= crit else ("elevated" if nr >= crit * 0.5 else "healthy")
        return (f"Null-amount rate is **{nr * 100:.0f}%** ({v}). The load stage refuses to publish above "
                f"{crit * 100:.0f}% — that would silently report $0 revenue, so I flag it well before then.")

    # throughput / volume / transactions --------------------------------------
    if has("throughput", "volume", "records", "traffic", "transaction", "how many", "starv"):
        rc = sig.get("record_count")
        if rc is None:
            return "No throughput reading yet."
        mn = config.MIN_RECORDS
        v = "starved" if rc < mn else "healthy"
        tp = sig.get("throughput_rps")
        return (f"The last run processed **{rc} records** ({v}; the starvation line is {mn})"
                + (f" at ~{tp} rec/s" if isinstance(tp, (int, float)) else "")
                + f". Distinct products seen: {sig.get('distinct_products', '?')}.")

    # revenue ------------------------------------------------------------------
    if has("revenue", "money", "dollar", "earning", "sales", "$"):
        rev = (ctx.get("totals") or {}).get("revenue") or 0
        last_rev = sig.get("revenue")
        return (f"Warehouse revenue total is **${rev:,.0f}**"
                + (f"; the last ETL run added ${last_rev:,.0f}." if isinstance(last_rev, (int, float)) else "."))

    # drift / schema -----------------------------------------------------------
    if has("drift", "schema", "field", "format", "structure"):
        return ("**Schema drift detected** — an upstream field changed shape and the parser will start "
                "dropping records. Resolve the field aliases before the null-rate zeroes revenue."
                if sig.get("schema_drift")
                else "No schema drift — the upstream field layout matches the learned baseline.")

    # audit --------------------------------------------------------------------
    if has("audit", "integrity", "tamper", "chain", "trail"):
        return ("The audit hash-chain is **intact** — every prediction, warning and governed action is "
                "tamper-evident." if ctx.get("audit_intact")
                else "The audit hash-chain is **BROKEN** — a record was altered; investigate immediately.")

    # health / status / catch-all ---------------------------------------------
    if has("health", "status", "how are", "everything ok", "are we ok", "summary", "overview", "doing"):
        return _full_status(ctx)

    return _full_status(ctx) + " (Ask me about risk, the latency SLA, data quality, throughput, revenue, or what to do.)"


def _answer_question(q: str) -> str:
    q = (q or "").strip()[:500]
    if not q:
        return "Ask me about the pipeline's risk, the SLA, data quality, or an incident's root cause."
    ctx = _chat_context()
    ql = " ".join(q.lower().split())
    is_greeting = ql in ("hi", "hello", "hey", "yo", "hola", "thanks", "thank you",
                         "ty", "thx", "sup", "gm") or any(
        ql == g or ql.startswith(g + " ") for g in _GREETINGS)
    # Greetings stay instant; every real question is reasoned by Opus 4.8 so it can
    # answer anything — live telemetry OR general (e.g. "what does RCA stand for?").
    if not is_greeting and config.CHAT_USE_LLM:
        brain = _get_brain()
        if brain is not None and getattr(brain, "available", False):
            user = (f"Operator question: {q}\n\nLive telemetry (JSON):\n"
                    f"{json.dumps(ctx, default=str)}")
            try:
                reply = brain.chat(_CHAT_SYSTEM, user).strip()
                if reply:
                    return reply
            except BrainError:
                pass
            except Exception:  # noqa: BLE001 - a chat hiccup must never crash the dashboard
                pass
    return _grounded_answer(q, ctx)


def _write_pending_incident(payload: dict) -> dict:
    if payload.get("reset"):
        # Bulletproof CLEAR: return the pipeline to healthy immediately AND durably.
        #  * write the GREEN banner now (instant UI feedback),
        #  * wipe the ramp state so no engine can re-assert the old fault,
        #  * drop the incident RCA,
        #  * leave a reset sentinel the live engine consumes to reset its in-memory
        #    mode on its next tick.
        try:
            config.INCIDENTS_FILE.write_text(json.dumps({"level": "GREEN"}),
                                             encoding="utf-8")
        except OSError:
            pass
        try:
            config.GUARDIAN_STATE_FILE.unlink()
        except OSError:
            pass
        try:
            PENDING_INCIDENT.write_text(json.dumps({"reset": True, "label": "clear"}),
                                        encoding="utf-8")
        except OSError:
            pass
        return {"status": "cleared"}
    if payload.get("apply_fix"):
        # Operator performed the AI's recommended remediation. Hand the live engine
        # a sentinel it consumes to DECAY the fault to 0 over a few ticks — the
        # dashboard then shows the pipeline recovering to GREEN in real time.
        label = str(payload.get("label") or "recommended fix")[:80]
        try:
            PENDING_INCIDENT.write_text(
                json.dumps({"apply_fix": True, "label": label}), encoding="utf-8")
        except OSError as exc:
            return {"status": "error", "detail": str(exc)}
        return {"status": "fix_applied", "label": label}
    ops = payload.get("ops")
    if not isinstance(ops, list) or not ops:
        return {"status": "error", "detail": "need a non-empty 'ops' list"}
    spec = {"label": str(payload.get("label") or "custom incident")[:80],
            "ops": [op for op in ops if isinstance(op, dict)][:20]}
    if not spec["ops"]:
        return {"status": "error", "detail": "ops must be objects"}
    try:
        PENDING_INCIDENT.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "detail": str(exc)}
    return {"status": "queued", "label": spec["label"], "ops": len(spec["ops"])}


# ---------------------------------------------------------------------------
# Prometheus exposition
# ---------------------------------------------------------------------------
def render_metrics() -> str:
    s = build_summary()
    t = s["totals"]
    last = s["last"] or {}

    def g(name: str, value, help_: str, typ: str = "gauge") -> str:
        try:
            v = float(value if value is not None else 0)
        except (TypeError, ValueError):
            v = 0.0
        return f"# HELP {name} {help_}\n# TYPE {name} {typ}\n{name} {v}\n"

    out = ""
    out += g("guardian_risk_score", last.get("risk_score"), "Latest predicted risk (0-100)")
    out += g("guardian_pipeline_health", last.get("pipeline_health"), "Latest pipeline health (0-100)")
    out += g("guardian_lead_time_minutes", last.get("lead_time_minutes"), "Predicted lead time (min)")
    out += g("guardian_confidence", last.get("confidence"), "Latest prediction confidence (0-1)")
    out += g("guardian_predictions_total", t["predictions"], "Predictions made", "counter")
    out += g("guardian_warnings_total", t["warnings"], "AMBER/RED early warnings", "counter")
    out += g("guardian_issues_opened_total", t["issues"], "Predicted-incident issues opened", "counter")
    out += g("guardian_prs_opened_total", t["prs"], "Gated preventive PRs opened", "counter")
    out += g("guardian_etl_failures_total", t["etl_failures"], "ETL failures observed", "counter")
    out += g("guardian_events_total", t["events"], "Audit records", "counter")
    out += g("guardian_revenue_total", t["revenue"], "Warehouse revenue (USD)")
    out += g("guardian_audit_intact", 1 if s["audit"]["intact"] else 0, "Audit hash-chain intact (1/0)")

    # --- AI-brain performance, pipeline health & live throughput series ---
    events = audit_trail.load_events()
    last_etl = _last_event(events, "etl_run")
    last_reason = _last_event(events, "agent_reason")
    preds = [e for e in events if e.get("event") == "prediction"]
    llm_preds = sum(1 for e in preds if ":" in str(e.get("source", "")))
    heur_preds = sum(1 for e in preds if str(e.get("source", "")) == "heuristic")
    level_num = {"GREEN": 0, "AMBER": 1, "RED": 2}.get(
        str((s["banner"] or {}).get("level", "GREEN")).upper(), 0)
    st = _stream_totals()

    out += g("guardian_incident_level", level_num, "Incident banner level (0 green,1 amber,2 red)")
    out += g("guardian_brain_available", 1 if last_reason.get("available") else 0, "AI brain reachable (1/0)")
    out += g("guardian_llm_predictions_total", llm_preds, "Predictions reasoned by the LLM brain", "counter")
    out += g("guardian_heuristic_predictions_total", heur_preds, "Predictions served by the heuristic", "counter")
    out += g("guardian_etl_records", last_etl.get("record_count"), "Records in the last ETL run")
    out += g("guardian_etl_null_rate", last_etl.get("null_rate"), "Null-amount rate in the last ETL run (0-1)")
    out += g("guardian_etl_revenue", last_etl.get("revenue"), "Revenue produced by the last ETL run (USD)")
    out += g("guardian_etl_latency_ms", last_etl.get("latency_ms"), "Last ETL transform+load latency (ms)")
    out += g("guardian_stream_extract_total", st["extract"], "Live trades extracted by the stream tap", "counter")
    out += g("guardian_stream_load_total", st["load"], "Aggregated rows loaded by the stream tap", "counter")
    out += g("guardian_stream_cycles_total", st["cycles"], "Live stream cycles completed", "counter")
    out += g("guardian_stream_last_revenue", st["last_revenue"], "Revenue in the most recent stream cycle (USD)")
    return out


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
def _esc(x: object) -> str:
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_COLOR = {"GREEN": "#1f9d55", "AMBER": "#d98c00", "RED": "#c0392b"}


_INJECT_BUTTONS = (
    '<button class="alt" onclick=\'fillIncident("null the size field",[{op:"null_field",field:"size"}])\'>Null size</button>'
    '<button class="alt" onclick=\'fillIncident("rename price to px",[{op:"rename_field",field:"price",to:"px"}])\'>Rename price</button>'
    '<button class="alt" onclick=\'fillIncident("vendor renames size to quantity",[{op:"rename_field",field:"size",to:"quantity"}])\'>Rename size &rarr; quantity</button>'
    '<button class="alt" onclick=\'fillIncident("price outlier spike",[{op:"scale_field",field:"price",factor:50}])\'>Price x50</button>'
    '<button class="alt" onclick=\'fillIncident("freeze price (stale feed)",[{op:"freeze_field",field:"price"}])\'>Freeze price</button>'
    '<button class="alt" onclick=\'fillIncident("volume collapse",[{op:"shrink_batch"}])\'>Shrink batch</button>'
    '<button class="alt" onclick=\'fillIncident("duplicate storm",[{op:"duplicate"}])\'>Dup storm</button>'
    '<button class="alt" onclick=\'fillIncident("load latency",[{op:"latency",ms:800}])\'>Add latency</button>'
    '<button onclick=\'induce({reset:true})\'>Clear / Resolve</button>'
)

_SCRIPT = """
<script>
function cwToggle(){
  var cw=document.getElementById('cw'); if(!cw)return;
  var open=cw.classList.toggle('open');
  try{localStorage.setItem('guardianChatOpen',open?'1':'0');}catch(e){}
  if(open){var m=document.getElementById('cwmsgs');if(m)m.scrollTop=m.scrollHeight;var g=document.getElementById('gq');if(g)g.focus();}
}
function cwAppend(cls,text){
  var m=document.getElementById('cwmsgs'); if(!m)return null;
  var d=document.createElement('div'); d.className='cwmsg '+cls; d.textContent=text;
  m.appendChild(d); m.scrollTop=m.scrollHeight; return d;
}
async function askGuardian(){
  var el=document.getElementById('gq'); if(!el)return; var q=(el.value||'').trim(); if(!q){return;}
  el.value=''; cwAppend('u',q);
  var t=cwAppend('a','typing…'); if(t)t.classList.add('cwtyping');
  try{
    var r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q:q})});
    var j=await r.json();
    if(t){t.classList.remove('cwtyping'); t.textContent=j.answer||'(no answer)';}
  }catch(e){ if(t){t.classList.remove('cwtyping'); t.textContent='Sorry — I could not reach the guardian.';} }
  var m=document.getElementById('cwmsgs'); if(m)m.scrollTop=m.scrollHeight;
}
async function induce(spec){
  try{await fetch('/inject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(spec)});}catch(e){}
  location.reload();
}
function induceCustom(){
  var label=(document.getElementById('inclabel').value||'custom incident');
  var raw=(document.getElementById('incops').value||'').trim();
  if(!raw){alert('click a preset to load it, or paste an ops JSON array');return;}
  var ops; try{ops=JSON.parse(raw);}catch(e){alert('ops must be a valid JSON array');return;}
  induce({label:label,ops:ops});
}
function fillIncident(label, ops){
  var l=document.getElementById('inclabel'); if(l)l.value=label;
  var t=document.getElementById('incops'); if(t){t.value=JSON.stringify(ops);t.focus();}
}
async function approveGov(){
  var b=document.getElementById('apprbtn'); if(b){b.disabled=true;b.textContent='Filing…';}
  try{await fetch('/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(e){}
  location.reload();
}
function applyFix(label){
  var btns=document.querySelectorAll('button.fix');
  for(var i=0;i<btns.length;i++){btns[i].disabled=true;btns[i].textContent='Applying — watch it recover…';}
  induce({apply_fix:true,label:label});
}
// Restore the chat open/closed state so it survives the live auto-refresh.
(function(){try{if(localStorage.getItem('guardianChatOpen')==='1'){var cw=document.getElementById('cw');if(cw){cw.classList.add('open');var m=document.getElementById('cwmsgs');if(m)m.scrollTop=m.scrollHeight;}}}catch(e){}})();
// Live auto-refresh every 5s for the dashboard panels — but NEVER while the chat
// is open or while you're typing in the custom-incident boxes.
(function(){
  var FIELDS=['incops','inclabel'], GRACE=30000, last=0;
  function touch(){last=Date.now();}
  FIELDS.forEach(function(id){var el=document.getElementById(id);if(el){el.addEventListener('input',touch);el.addEventListener('keydown',touch);el.addEventListener('focus',touch);}});
  function busy(){
    try{if(localStorage.getItem('guardianChatOpen')==='1')return true;}catch(e){}
    var ae=document.activeElement;
    for(var i=0;i<FIELDS.length;i++){if(document.getElementById(FIELDS[i])===ae)return true;}
    return (Date.now()-last)<GRACE;
  }
  setInterval(function(){if(!busy())location.reload();},5000);
})();
</script>
"""


def _fix_action_label(failure_type: object) -> str:
    """The concrete remediation the operator performs for a predicted failure —
    shown on the banner's 'Apply fix' button."""
    ft = str(failure_type or "").lower()
    if "latency" in ft or "timeout" in ft or "slow" in ft:
        return "Scale out consumers"
    if ("volume" in ft or "throughput" in ft or "stall" in ft
            or "drought" in ft or "starv" in ft):
        return "Restore upstream feed & backfill"
    if "schema" in ft or "parse" in ft or "rename" in ft or "drift" in ft:
        return "Apply the merged parser fix"
    if "dup" in ft:
        return "Apply the merged dedupe fix"
    if ("null" in ft or "quality" in ft or "missing" in ft
            or "corrupt" in ft or "type" in ft):
        return "Apply the merged data-quality fix"
    return "Apply the recommended fix"


def render_html() -> str:
    s = build_summary()
    t = s["totals"]
    a = s["audit"]
    last = s["last"] or {}
    level = (s["banner"] or {}).get("level", "GREEN")
    color = _COLOR.get(level, "#1f9d55")
    banner = s["banner"] or {}
    pred = banner.get("prediction", {}) if isinstance(banner, dict) else {}

    # Real-time monitoring strip — latency vs SLA, risk trend, throughput/quality
    hist = _signal_history(40)
    sla = _sla_view(hist)
    lat_series = _series(hist, "latency_ms")
    rec_series = _series(hist, "record_count")
    risk_series = [p.get("risk_score") for p in reversed(s["recent_predictions"])
                   if isinstance(p.get("risk_score"), (int, float))]
    realtime_html = _render_realtime(sla, lat_series, risk_series, rec_series, hist)
    rca_html = _render_rca_html(level)

    def card(label: str, value: object, sub: str = "") -> str:
        return (f'<div class="card"><div class="v">{_esc(value)}</div>'
                f'<div class="l">{_esc(label)}</div>'
                f'{f"<div class=s>{_esc(sub)}</div>" if sub else ""}</div>')

    banner_body = ""
    if level != "GREEN":
        awaiting = banner.get("awaiting_approval")
        approved = banner.get("approved")
        links = ""
        if banner.get("issue_url"):
            links += f' &nbsp;·&nbsp; <a href="{_esc(banner["issue_url"])}" target="_blank">predicted-incident issue</a>'
        if banner.get("pr_url"):
            links += f' &nbsp;·&nbsp; <a href="{_esc(banner["pr_url"])}" target="_blank">gated preventive PR (awaiting human merge)</a>'
        apply_label = _fix_action_label(pred.get("predicted_failure_type"))
        apply_html = (
            '<div class="row"><button class="fix" onclick="applyFix(\''
            + _esc(apply_label) + '\')">⚙ Apply fix: ' + _esc(apply_label) + '</button>'
            '<span class="fixhint">Runs the recommended remediation — watch the '
            'pipeline recover to GREEN.</span></div>')
        if awaiting:
            action = ('<div class="appr">⏸ <b>Awaiting your approval.</b> The AI predicted this and wrote '
                      'the root-cause analysis + recommended fix (below), but has filed <b>nothing</b> yet. '
                      'Review it, then approve to open the AI-written governed issue + gated preventive PR '
                      '— nothing is ever merged or auto-fixed without you.'
                      '<div class="row"><button id="apprbtn" onclick="approveGov()">✓ Approve &amp; file governed issue / PR</button>'
                      '<button class="alt" onclick="induce({reset:true})">Dismiss (I\'ll fix it manually)</button></div>'
                      + apply_html + '</div>')
        elif approved:
            action = (f'<div class="appr ok">✓ Approved by you — AI-written governed issue / PR filed.{links}'
                      + apply_html + '</div>')
        else:
            if links:
                action = ('<div class="appr ok">🤖 <b>AI auto-raised this incident.</b> '
                          'The governed issue'
                          + (' + gated code-fix PR' if banner.get("pr_url") else '')
                          + ' was filed automatically — review &amp; <b>merge the PR</b> to '
                          'productionize the fix, or <b>Apply fix</b> below to recover the live '
                          'pipeline now.' + links + '</div>' + apply_html)
            else:
                action = ('<div class="appr">🤖 <b>AI is auto-filing the GitHub issue</b> '
                          '(+ a gated code-fix PR if this needs a code change)… no approval '
                          'needed. You can <b>Apply fix</b> below now.' + apply_html)
        banner_body = (
            f'<div class="bnr-sub">Predicted <b>{_esc(pred.get("predicted_failure_type", "failure"))}</b>'
            f' — risk {_esc(pred.get("risk_score"))}/100, ~{_esc(pred.get("lead_time_minutes"))} min lead time.</div>'
            + action
        )
    else:
        banner_body = '<div class="bnr-sub">Live pipeline healthy — watching for early risk.</div>'

    rows = ""
    for p in s["recent_predictions"]:
        lvl = p.get("level", "GREEN")
        rows += (
            f'<tr><td>{_esc(p.get("ts","")[11:19])}</td>'
            f'<td><span class="pill" style="background:{_COLOR.get(lvl,"#1f9d55")}">{_esc(lvl)}</span></td>'
            f'<td>{_esc(p.get("risk_score"))}</td>'
            f'<td>{_esc(p.get("pipeline_health"))}</td>'
            f'<td>{_esc(p.get("lead_time_minutes"))}m</td>'
            f'<td>{_esc(p.get("predicted_failure_type"))}</td>'
            f'<td><code>{_esc(p.get("source"))}</code></td></tr>'
        )
    rows = rows or '<tr><td colspan=7 class="muted">No predictions yet — trigger the DAG or run the demo.</td></tr>'

    gov = ""
    for i in s["recent_issues"]:
        gov += (f'<div class="gv"><span class="tag issue">ISSUE</span> '
                f'<a href="{_esc(i.get("url"))}" target="_blank">{_esc(i.get("url"))}</a>'
                f' <span class="muted">({_esc(i.get("severity"))})</span></div>')
    for p in s["recent_prs"]:
        gov += (f'<div class="gv"><span class="tag pr">PR</span> '
                f'<a href="{_esc(p.get("url"))}" target="_blank">{_esc(p.get("url"))}</a>'
                f' <span class="muted">gated — human approves</span></div>')
    gov = gov or '<div class="muted">No governed issues/PRs yet. AMBER opens an issue; RED stages a gated PR.</div>'

    chain = ('<span class="ok">✓ INTACT</span>' if a["intact"]
             else f'<span class="bad">✗ BROKEN at #{a["broken_at"]}</span>')

    chat_msgs = ""
    for tn in _chat_load()[-14:]:
        chat_msgs += (f'<div class="cwmsg u">{_esc(tn.get("q", ""))}</div>'
                      f'<div class="cwmsg a">{_esc(tn.get("a", ""))}</div>')
    chat_msgs = chat_msgs or (
        '<div class="cwmsg a">Hi! I am the Guardian. Ask me &ldquo;what is the biggest '
        'risk right now?&rdquo;, &ldquo;are we breaching the latency SLA?&rdquo;, or '
        '&ldquo;explain the root cause&rdquo;.</div>')
    panels_html = (
        '<div class="sec">🧪 Induce your OWN incident (unknown to the AI)</div>'
        '<div class="panel">'
        '<div class="muted" style="margin-bottom:6px">Step 1 — click a preset to LOAD it into the box '
        '(it does <b>not</b> fire yet), or write your own ops. Step 2 — click <b>Induce custom incident</b> '
        'to actually inject it. <b>Clear / Resolve</b> ends the active incident.</div>'
        '<div class="row">' + _INJECT_BUTTONS + '</div>'
        "<textarea id=\"incops\" placeholder='ops JSON array, e.g. "
        "[{&quot;op&quot;:&quot;null_field&quot;,&quot;field&quot;:&quot;size&quot;},"
        "{&quot;op&quot;:&quot;latency&quot;,&quot;ms&quot;:800}]'></textarea>"
        '<div class="row"><input id="inclabel" placeholder="incident name (optional)">'
        '<button onclick="induceCustom()">Induce custom incident</button></div>'
        '<div class="muted" style="margin-top:6px">Once induced it lands on the next tick (a few seconds) and '
        'ramps. The agent is never told what you did — watch it predict the failure BEFORE it breaks.</div></div>'
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Predictive Pipeline Guardian</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0e1117;color:#e6edf3}}
 .wrap{{max-width:1100px;margin:0 auto;padding:22px}}
 h1{{font-size:20px;margin:0 0 2px}} .sub{{color:#8b949e;font-size:13px;margin-bottom:16px}}
 .bnr{{background:{color};border-radius:12px;padding:16px 20px;margin-bottom:18px}}
 .bnr b{{font-weight:700}} .bnr-title{{font-size:18px;font-weight:700}} .bnr-sub{{margin-top:6px;font-size:13px;opacity:.95}}
 .bnr a{{color:#fff;text-decoration:underline}}
 .appr{{background:rgba(0,0,0,.22);border-radius:8px;padding:10px 12px;margin-top:10px;font-size:13px;line-height:1.5}}
 .appr.ok{{background:rgba(0,0,0,.28)}} .appr .row{{margin-top:8px}} .appr a{{color:#fff}}
 .grid{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:18px}}
 .grid3{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}}
 .panel.sp .v{{font-size:24px;font-weight:700}} .panel.sp .l{{color:#8b949e;font-size:11px}}
 .panel.sp .s{{color:#8b949e;font-size:11px;margin-top:2px}}
 .spark{{margin-top:8px}} .spark svg{{width:100%;height:46px;display:block}}
 .rcagrid{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:10px}}
 .rcagrid ul,.rcagrid ol{{margin:4px 0 0 18px;padding:0}} .rcagrid li{{font-size:12px;margin:2px 0;color:#c9d3de}}
 .rcagrid>div>b{{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
 .rcabadge{{display:inline-block;font-size:11px;font-weight:700;color:#fff;padding:3px 9px;border-radius:8px;margin-bottom:8px}}
 .rcabadge.red{{background:#c0392b}}
 .rcaok{{color:#3fb950;font-size:13px;font-weight:600}}
 .rcaitem{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:10px 12px;margin-bottom:8px}}
 .rcaitem>summary{{cursor:pointer;font-size:13px;font-weight:600;list-style:none}}
 .rcaitem>summary::-webkit-details-marker{{display:none}}
 .rcaitem[open]>summary{{margin-bottom:8px;border-bottom:1px solid #21262d;padding-bottom:8px}}
 .rcabody{{font-size:13px;color:#c9d3de}} .rcabody .s{{color:#8b949e}} .rcabody .a{{color:#9fb6cf;margin-top:3px;white-space:pre-wrap}}
 .rcabody b{{color:#e6edf3}}
 .rcastack{{margin-bottom:12px}} .rcastack>summary{{cursor:pointer;color:#8b949e;font-size:12px;padding:8px 0;user-select:none}}
 .card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px}}
 .card .v{{font-size:22px;font-weight:700}} .card .l{{color:#8b949e;font-size:11px;margin-top:2px}} .card .s{{color:#6e7681;font-size:10px}}
 table{{width:100%;border-collapse:collapse;background:#161b22;border-radius:10px;overflow:hidden}}
 th,td{{text-align:left;padding:8px 10px;font-size:12px;border-bottom:1px solid #21262d}}
 th{{color:#8b949e;font-weight:600}} code{{color:#79c0ff}}
 .pill{{color:#fff;padding:1px 8px;border-radius:10px;font-size:11px}}
 .sec{{margin:20px 0 8px;font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}}
 .gv{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:8px 10px;margin-bottom:6px;font-size:12px}}
 .tag{{padding:1px 6px;border-radius:6px;color:#fff;font-size:10px;margin-right:6px}}
 .tag.issue{{background:#8957e5}} .tag.pr{{background:#1f6feb}}
 .muted{{color:#6e7681}} .ok{{color:#3fb950;font-weight:700}} .bad{{color:#f85149;font-weight:700}}
 a{{color:#79c0ff}}
 .panel{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px;margin-bottom:10px}}
 .qa{{border-bottom:1px solid #21262d;padding:6px 0;font-size:13px}}
 .qa .q{{color:#e6edf3}} .qa .a{{color:#9fb6cf;margin-top:3px;white-space:pre-wrap}}
 .row{{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center}}
 input,textarea{{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:8px;font-family:inherit;font-size:13px}}
 #gq{{flex:1;min-width:280px}} #incops{{width:100%;min-height:60px;margin-top:6px}}
 button{{background:#1f6feb;color:#fff;border:none;border-radius:8px;padding:8px 12px;cursor:pointer;font-size:13px}}
 button.alt{{background:#30363d}} button:disabled{{opacity:.6}}
 button.fix{{background:#1f9d55;font-weight:600}} button.fix:hover{{background:#25b365}}
 .fixhint{{font-size:12px;opacity:.85}}
 .cw{{position:fixed;bottom:20px;right:20px;z-index:9999;display:flex;flex-direction:column;align-items:flex-end}}
 .cwbtn{{display:flex;align-items:center;gap:8px;background:#1f6feb;color:#fff;border-radius:28px;padding:11px 18px;font-size:20px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.45)}}
 .cwlabel{{font-size:14px;font-weight:600}}
 .cw.open .cwbtn{{display:none}}
 .cwpanel{{display:none;flex-direction:column;width:370px;height:520px;max-height:72vh;background:#0d1117;border:1px solid #30363d;border-radius:16px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.55);margin-bottom:10px}}
 .cw.open .cwpanel{{display:flex}}
 .cwhead{{background:#161b22;padding:13px 16px;font-weight:700;font-size:14px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center}}
 .cwx{{cursor:pointer;color:#8b949e;font-size:22px;line-height:1}}
 .cwmsgs{{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:9px}}
 .cwmsg{{max-width:88%;padding:9px 12px;border-radius:13px;font-size:13px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}}
 .cwmsg.u{{align-self:flex-end;background:#1f6feb;color:#fff;border-bottom-right-radius:3px}}
 .cwmsg.a{{align-self:flex-start;background:#21262d;color:#e6edf3;border-bottom-left-radius:3px}}
 .cwtyping{{color:#8b949e;font-style:italic}}
 .cwinput{{display:flex;gap:8px;padding:11px;border-top:1px solid #30363d;background:#161b22}}
 .cwinput input{{flex:1;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:22px;padding:10px 15px;font-size:13px}}
 .cwinput button{{width:40px;height:40px;min-width:40px;border-radius:50%;background:#1f6feb;color:#fff;border:none;cursor:pointer;font-size:15px;padding:0}}
</style></head><body><div class="wrap">
 <h1>🔮 Predictive Pipeline Guardian</h1>
 <div class="sub">Predicts data-pipeline failures BEFORE they happen · grounded predictive AI · Opus 4.8 RCA · governed by a human · updated {_esc(s["generated_at"][11:19])} UTC</div>
 <div class="bnr"><div class="bnr-title">{_esc(level)}</div>{banner_body}</div>
 {realtime_html}
 {rca_html}
 <div class="grid">
   {card("Latest risk", f'{last.get("risk_score","–")}/100')}
   {card("Pipeline health", f'{last.get("pipeline_health","–")}/100')}
   {card("Lead time", f'{last.get("lead_time_minutes","–")} min')}
   {card("Early warnings", t["warnings"])}
   {card("Issues opened", t["issues"])}
   {card("Gated PRs", t["prs"])}
 </div>
 <div class="grid">
   {card("Predictions", t["predictions"])}
   {card("ETL failures", t["etl_failures"])}
   {card("Revenue (USD)", f'{t["revenue"]:,}')}
   {card("Audit records", a["records"])}
   {card("Audit chain", "", "")}
   {card("Confidence", last.get("confidence","–"))}
 </div>
 <div class="sec">Audit integrity: {chain}</div>
 <div class="sec">Prediction stream</div>
 <table><tr><th>Time</th><th>Level</th><th>Risk</th><th>Health</th><th>Lead</th><th>Predicted failure</th><th>Brain</th></tr>{rows}</table>
 <div class="sec">Governance — predicted-incident issues & gated preventive PRs</div>
 {gov}
 {panels_html}
</div>
<div id="cw" class="cw">
  <div class="cwpanel">
    <div class="cwhead"><span>🔮 Ask the Guardian</span><span class="cwx" onclick="cwToggle()">×</span></div>
    <div id="cwmsgs" class="cwmsgs">{chat_msgs}</div>
    <div class="cwinput"><input id="gq" autocomplete="off" placeholder="Ask about risk, SLA, or the RCA…" onkeydown="if(event.key==='Enter')askGuardian()"><button id="gqbtn" onclick="askGuardian()">➤</button></div>
  </div>
  <div class="cwbtn" onclick="cwToggle()">💬<span class="cwlabel">Ask the Guardian</span></div>
</div>
{_SCRIPT}</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/metrics"):
            body = render_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
        elif self.path.startswith("/api"):
            body = json.dumps(build_summary(), default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = render_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if self.path.startswith("/alert"):
            # Grafana alert webhook -> record it to the live log stream.
            try:
                _record_grafana_alert(payload)
            except Exception:  # noqa: BLE001 - never let a webhook crash the server
                pass
            body = b'{"status":"received"}'
            code = 200
        elif self.path.startswith("/chat"):
            answer = _answer_question(str(payload.get("q", "")))
            turns = _chat_load()
            turns.append({"ts": datetime.now(timezone.utc).isoformat(),
                          "q": str(payload.get("q", ""))[:500], "a": answer})
            _chat_save(turns)
            body = json.dumps({"answer": answer}).encode("utf-8")
            code = 200
        elif self.path.startswith("/inject"):
            body = json.dumps(_write_pending_incident(payload)).encode("utf-8")
            code = 200
        elif self.path.startswith("/approve"):
            # Human-in-the-loop: file the governed issue / gated PR ONLY on this click.
            try:
                result = notifier.approve_active_incident()
            except Exception:  # noqa: BLE001 - approval must never crash the server
                result = {"status": "error"}
            body = json.dumps(result).encode("utf-8")
            code = 200
        else:
            body = b'{"error":"not found"}'
            code = 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = config.DASHBOARD_PORT
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[dashboard] Predictive Guardian live monitor on http://localhost:{port}", flush=True)
    print(f"[dashboard] Prometheus metrics on http://localhost:{port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
