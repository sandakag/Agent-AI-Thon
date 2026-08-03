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
    "You are the assistant for a Predictive Pipeline Guardian — an AI SRE that "
    "PREDICTS data-pipeline failures BEFORE they happen. Answer the operator's "
    "question about the LIVE pipeline using ONLY the telemetry provided. Be "
    "concise, concrete and forward-looking: what is likely to break, WHEN (lead "
    "time), WHY (which signals), and how to PREVENT it. If the telemetry does not "
    "support an answer, say so plainly. Never invent numbers."
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


def _chat_context() -> dict:
    s = build_summary()
    signals = {}
    if config.SIGNAL_HISTORY_FILE.exists():
        try:
            hist = json.loads(config.SIGNAL_HISTORY_FILE.read_text(encoding="utf-8"))
            if hist:
                signals = hist[-1]
        except (json.JSONDecodeError, OSError):
            pass
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
        "recent_failure_types": [p.get("predicted_failure_type")
                                 for p in s.get("recent_predictions", [])][:8],
        "totals": s.get("totals"),
        "audit_intact": (s.get("audit") or {}).get("intact"),
    }


def _fallback_answer(ctx: dict) -> str:
    p = ctx.get("latest_prediction") or {}
    lvl = ctx.get("banner_level") or "GREEN"
    parts = [f"[grounded answer — AI brain offline] Status {lvl}, risk "
             f"{p.get('risk_score')}/100."]
    ft = p.get("predicted_failure_type")
    if ft and ft != "none":
        parts.append(f"Predicted failure: {ft} (~{p.get('lead_time_minutes')} min lead).")
    ev = p.get("evidence") or []
    if ev:
        parts.append("Why: " + "; ".join(str(e) for e in ev[:3]) + ".")
    if p.get("recommended_action"):
        parts.append(f"Prevention: {p['recommended_action']}")
    if lvl == "GREEN":
        parts.append("Nothing is failing now; the guardian is watching the trends.")
    return " ".join(parts)


def _answer_question(q: str) -> str:
    q = (q or "").strip()[:500]
    if not q:
        return "Ask about the pipeline's risk, what's likely to fail, when, or why."
    ctx = _chat_context()
    brain = _get_brain()
    if brain is not None and getattr(brain, "available", False):
        user = (f"Operator question: {q}\n\nLive telemetry (JSON):\n"
                f"{json.dumps(ctx, default=str)}")
        try:
            reply = brain.chat(_CHAT_SYSTEM, user).strip()
            return reply or _fallback_answer(ctx)
        except BrainError:
            return _fallback_answer(ctx)
        except Exception:  # noqa: BLE001 - a chat hiccup must never crash the dashboard
            return _fallback_answer(ctx)
    return _fallback_answer(ctx)


def _write_pending_incident(payload: dict) -> dict:
    if payload.get("reset"):
        try:
            PENDING_INCIDENT.unlink(missing_ok=True)
        except OSError:
            pass
        return {"status": "cleared"}
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
    '<button class="alt" onclick=\'induce({label:"null the size field",ops:[{op:"null_field",field:"size"}]})\'>Null size</button>'
    '<button class="alt" onclick=\'induce({label:"rename price to px",ops:[{op:"rename_field",field:"price",to:"px"}]})\'>Rename price</button>'
    '<button class="alt" onclick=\'induce({label:"price outlier spike",ops:[{op:"scale_field",field:"price",factor:50}]})\'>Price x50</button>'
    '<button class="alt" onclick=\'induce({label:"freeze price (stale feed)",ops:[{op:"freeze_field",field:"price"}]})\'>Freeze price</button>'
    '<button class="alt" onclick=\'induce({label:"volume collapse",ops:[{op:"shrink_batch"}]})\'>Shrink batch</button>'
    '<button class="alt" onclick=\'induce({label:"duplicate storm",ops:[{op:"duplicate"}]})\'>Dup storm</button>'
    '<button class="alt" onclick=\'induce({label:"load latency",ops:[{op:"latency",ms:1500}]})\'>Add latency</button>'
    '<button class="alt" onclick=\'induce({reset:true})\'>Clear</button>'
)

_SCRIPT = """
<script>
async function askGuardian(){
  var el=document.getElementById('gq'); var q=(el.value||'').trim(); if(!q){return;}
  var b=document.getElementById('gqbtn'); b.disabled=true; b.textContent='Thinking…';
  try{await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q:q})});}catch(e){}
  location.reload();
}
async function induce(spec){
  try{await fetch('/inject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(spec)});}catch(e){}
  location.reload();
}
function induceCustom(){
  var label=(document.getElementById('inclabel').value||'custom incident');
  var raw=(document.getElementById('incops').value||'').trim();
  if(!raw){alert('paste an ops JSON array, or use the quick buttons');return;}
  var ops; try{ops=JSON.parse(raw);}catch(e){alert('ops must be a valid JSON array');return;}
  induce({label:label,ops:ops});
}
</script>
"""


def render_html() -> str:
    s = build_summary()
    t = s["totals"]
    a = s["audit"]
    last = s["last"] or {}
    level = (s["banner"] or {}).get("level", "GREEN")
    color = _COLOR.get(level, "#1f9d55")
    banner = s["banner"] or {}
    pred = banner.get("prediction", {}) if isinstance(banner, dict) else {}

    def card(label: str, value: object, sub: str = "") -> str:
        return (f'<div class="card"><div class="v">{_esc(value)}</div>'
                f'<div class="l">{_esc(label)}</div>'
                f'{f"<div class=s>{_esc(sub)}</div>" if sub else ""}</div>')

    banner_body = ""
    if level != "GREEN":
        links = ""
        if banner.get("issue_url"):
            links += f' &nbsp;·&nbsp; <a href="{_esc(banner["issue_url"])}" target="_blank">predicted-incident issue</a>'
        if banner.get("pr_url"):
            links += f' &nbsp;·&nbsp; <a href="{_esc(banner["pr_url"])}" target="_blank">gated preventive PR (awaiting approval)</a>'
        banner_body = (
            f'<div class="bnr-sub">Predicted <b>{_esc(pred.get("predicted_failure_type", "failure"))}</b>'
            f' — risk {_esc(pred.get("risk_score"))}/100, ~{_esc(pred.get("lead_time_minutes"))} min lead time.{links}</div>'
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

    chat_rows = ""
    for tn in _chat_load()[-6:]:
        chat_rows += (f'<div class="qa"><div class="q">🧑 {_esc(tn.get("q", ""))}</div>'
                      f'<div class="a">🤖 {_esc(tn.get("a", ""))}</div></div>')
    chat_rows = chat_rows or ('<div class="muted">Ask the guardian anything about the '
                              'live pipeline — e.g. “what is the biggest risk right now '
                              'and when will it break?”</div>')
    panels_html = (
        '<div class="sec">💬 Ask the Guardian (GitHub Copilot brain)</div>'
        '<div class="panel">' + chat_rows +
        '<div class="row">'
        '<input id="gq" placeholder="what is the biggest risk right now and when will it break?" '
        "onkeydown=\"if(event.key==='Enter')askGuardian()\">"
        '<button id="gqbtn" onclick="askGuardian()">Ask</button></div>'
        '<div class="muted" style="margin-top:6px">Grounded in live telemetry; '
        'answers can take a few seconds (Copilot brain).</div></div>'
        '<div class="sec">🧪 Induce your OWN incident (unknown to the AI)</div>'
        '<div class="panel"><div class="row">' + _INJECT_BUTTONS + '</div>'
        "<textarea id=\"incops\" placeholder='advanced: ops JSON array, e.g. "
        "[{&quot;op&quot;:&quot;null_field&quot;,&quot;field&quot;:&quot;size&quot;},"
        "{&quot;op&quot;:&quot;latency&quot;,&quot;ms&quot;:1200}]'></textarea>"
        '<div class="row"><input id="inclabel" placeholder="incident name (optional)">'
        '<button onclick="induceCustom()">Induce custom incident</button></div>'
        '<div class="muted" style="margin-top:6px">Lands in the next live '
        '<code>run_demo.py</code> tick. The agent is never told what you did — '
        'watch it predict the failure.</div></div>'
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>Predictive Pipeline Guardian</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0e1117;color:#e6edf3}}
 .wrap{{max-width:1100px;margin:0 auto;padding:22px}}
 h1{{font-size:20px;margin:0 0 2px}} .sub{{color:#8b949e;font-size:13px;margin-bottom:16px}}
 .bnr{{background:{color};border-radius:12px;padding:16px 20px;margin-bottom:18px}}
 .bnr b{{font-weight:700}} .bnr-title{{font-size:18px;font-weight:700}} .bnr-sub{{margin-top:6px;font-size:13px;opacity:.95}}
 .bnr a{{color:#fff;text-decoration:underline}}
 .grid{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:18px}}
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
</style></head><body><div class="wrap">
 <h1>🔮 Predictive Pipeline Guardian</h1>
 <div class="sub">Predicts data-pipeline failures BEFORE they happen · GitHub Copilot brain · governed by a human · updated {_esc(s["generated_at"][11:19])} UTC</div>
 <div class="bnr"><div class="bnr-title">{_esc(level)}</div>{banner_body}</div>
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
</div>{_SCRIPT}</body></html>"""


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
