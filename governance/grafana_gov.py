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

import config


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

    body = json.dumps(
        {"tags": ["guardian", level.lower(), ftype], "text": text}
    ).encode("utf-8")

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
