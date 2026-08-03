"""Governor -> Grafana bridge (dependency-free).

When the preventive policy raises an early warning, the governor drops a
**Grafana annotation** at that exact instant — tagged ``guardian`` plus the risk
level — so every dashboard (AI, pipeline, OpenTelemetry) shows a vertical marker
on the precise moment the predicted incident was flagged. An injected production
issue therefore becomes visually obvious across all boards at a glance.

Best-effort and fully non-fatal: if Grafana is unreachable the guardian carries
on unaffected. Auth uses the provisioned admin credentials (basic auth), which
also works against the stack's anonymous-Admin Grafana.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import config
from agent import audit_trail

_STORE = config.DATA_DIR / "grafana_incidents.json"


def _auth_header() -> str:
    raw = f"{config.GRAFANA_USER}:{config.GRAFANA_PASSWORD}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def annotate(prediction: dict, decision: dict) -> str | None:
    """POST a Grafana annotation marking a predicted incident.

    Returns the annotation id (as a string) on success, or ``None`` on any
    failure (unreachable Grafana, timeout, bad response) — never raises.
    """
    base = (config.GRAFANA_URL or "").rstrip("/")
    if not base:
        return None

    level = str(decision.get("level", "AMBER"))
    ftype = str(prediction.get("predicted_failure_type", "failure"))
    risk = prediction.get("risk_score", "?")
    lead = prediction.get("lead_time_minutes", "?")
    text = (
        f"{level}: predicted <b>{ftype}</b> — risk {risk}/100, "
        f"~{lead} min lead time. {decision.get('recommendation', '')}"
    ).strip()

    return _post_annotation(["guardian", level.lower(), ftype], text)


def _post_annotation(tags: list[str], text: str) -> str | None:
    """POST a Grafana annotation (the dashboard incident marker). Never raises."""
    base = (config.GRAFANA_URL or "").rstrip("/")
    if not base:
        return None
    body = json.dumps({"tags": tags, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/annotations",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": _auth_header()},
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return str(payload.get("id")) if payload.get("id") is not None else "ok"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Grafana IRM incident — the REAL incident, declared ONCE per predicted failure
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def signature(prediction: dict) -> str:
    """Stable per-failure key used to de-duplicate incidents (mirrors github_gov)."""
    ft = str(prediction.get("predicted_failure_type") or "unknown").strip().lower()
    return "".join(c if c.isalnum() else "-" for c in ft).strip("-") or "unknown"


def _store_load() -> dict:
    if _STORE.exists():
        try:
            return json.loads(_STORE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _store_save(store: dict) -> None:
    try:
        _STORE.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def _analysis(prediction: dict, decision: dict, links: dict | None = None) -> str:
    """The AI's full incident analysis: severity, RCA evidence, remediation, links."""
    ev = prediction.get("evidence", [])[:6]
    ev_txt = "; ".join(ev) if ev else "signals within range"
    links = links or {}
    parts = []
    if links.get("issue_url"):
        parts.append(f'<a href="{links["issue_url"]}">predicted-incident issue</a>')
    if links.get("pr_url"):
        parts.append(f'<a href="{links["pr_url"]}">gated preventive PR</a>')
    link_txt = (" Governance: " + ", ".join(parts) + ".") if parts else ""
    return (
        f"<b>{decision.get('level')}</b> predicted "
        f"<b>{prediction.get('predicted_failure_type')}</b> — risk "
        f"{prediction.get('risk_score')}/100, health "
        f"{prediction.get('pipeline_health')}/100, ~"
        f"{prediction.get('lead_time_minutes')} min lead time. "
        f"RCA: {ev_txt}. Remediation: {prediction.get('recommended_action')}."
        f"{link_txt} Reasoned by {prediction.get('source')}."
    )


def _declare_irm_incident(prediction: dict, decision: dict, summary: str) -> str | None:
    """Declare a REAL incident via the Grafana Incident (IRM) API. Returns the
    incident id, or ``None`` when no token is set / the plugin is unavailable."""
    token = (config.GRAFANA_IRM_TOKEN or "").strip()
    base = (config.GRAFANA_IRM_URL or "").rstrip("/")
    if not token or not base:
        return None
    level = str(decision.get("level", "AMBER"))
    title = (
        f"[Predicted] {prediction.get('predicted_failure_type', 'pipeline failure')} "
        f"(risk {prediction.get('risk_score')}/100)"
    )
    body = json.dumps({
        "title": title[:250],
        "severity": "critical" if level == "RED" else "minor",
        "status": "active",
        "isDrill": False,
        "labels": [{"label": "predictive-guardian"}, {"label": level.lower()}],
        "summary": summary,
    }).encode("utf-8")
    url = (f"{base}/api/plugins/grafana-incident-app/resources/"
           f"api/v1/IncidentsService.CreateIncident")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    inc = payload.get("incident") if isinstance(payload, dict) else None
    if isinstance(inc, dict):
        return inc.get("incidentID") or inc.get("id")
    return None


def _resolve_irm_incident(incident_id: str) -> None:
    token = (config.GRAFANA_IRM_TOKEN or "").strip()
    base = (config.GRAFANA_IRM_URL or "").rstrip("/")
    if not token or not base or not incident_id:
        return
    body = json.dumps({"incidentID": incident_id, "status": "resolved"}).encode("utf-8")
    url = (f"{base}/api/plugins/grafana-incident-app/resources/"
           f"api/v1/IncidentsService.UpdateStatus")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    })
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def open_incident(prediction: dict, decision: dict, links: dict | None = None) -> str | None:
    """Declare a Grafana incident for a predicted failure — carrying the AI's full
    analysis and remediation, plus links to the governed issue / gated PR.

    De-duped per predicted-failure signature (one incident per episode). Always
    drops a rich incident annotation tagged ``guardian``+``incident`` (the
    guaranteed marker on this stack's OSS Grafana, shown on every board); when a
    Grafana IRM token is configured it ALSO declares a real incident via the
    Grafana Incident API. Records to the hash-chained audit trail. Never raises.
    """
    level = str(decision.get("level", "AMBER"))
    ftype = str(prediction.get("predicted_failure_type", "failure"))
    sig = signature(prediction)

    store = _store_load()
    if sig in store:
        audit_trail.audit("grafana_incident_deduped", signature=sig,
                          ref=store[sig].get("ref"))
        return store[sig].get("ref")

    summary = _analysis(prediction, decision, links)
    annotation_id = _post_annotation(["guardian", "incident", level.lower(), ftype], summary)
    irm_id = _declare_irm_incident(prediction, decision, summary)
    irm_ref = (f"{(config.GRAFANA_IRM_URL or '').rstrip('/')}"
               f"/a/grafana-incident-app/incidents/{irm_id}") if irm_id else None
    ref = irm_ref or (f"grafana-annotation:{annotation_id}" if annotation_id else None)

    store[sig] = {"ref": ref, "annotation": annotation_id, "irm_id": irm_id,
                  "level": level, "opened": _now()}
    _store_save(store)

    audit_trail.audit(
        "grafana_incident_opened", signature=sig, severity=level,
        predicted_failure_type=ftype, risk_score=prediction.get("risk_score"),
        lead_time_minutes=prediction.get("lead_time_minutes"),
        irm=bool(irm_id), annotation=annotation_id or "n/a", ref=ref,
    )
    kind = "IRM incident" if irm_id else "incident annotation"
    tail = f": {irm_ref}" if irm_ref else ""
    print(f"    -> [governance] Grafana {kind} declared for {ftype} "
          f"({level}, risk {prediction.get('risk_score')}/100){tail}")
    return ref


def resolve_incidents() -> None:
    """Resolve any open Grafana incidents (pipeline back to GREEN / ramp reset).
    Clears the de-dupe store, resolves real IRM incidents when configured, and
    drops a resolved marker on every board. Never raises."""
    store = _store_load()
    if not store:
        return
    for rec in store.values():
        irm_id = rec.get("irm_id")
        if irm_id:
            _resolve_irm_incident(irm_id)
    _post_annotation(["guardian", "incident", "resolved"],
                     "Guardian incident(s) resolved — pipeline back to GREEN.")
    audit_trail.audit("grafana_incident_resolved", count=len(store),
                      signatures=list(store.keys()))
    _store_save({})
