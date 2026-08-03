"""Early-warning + governance output.

Prints a clean console panel, appends a **hash-chained** audit line (via
``agent.audit_trail``), and persists the active-incident banner the live
dashboard reads. When ``GITHUB_TOKEN`` + ``GITHUB_REPOSITORY`` are configured it
files a **real** predicted-incident issue (AMBER+) and a **real gated preventive
PR** (RED) — both de-duplicated per predicted-failure signature. The AI does the
legwork and STOPS; a human approves any merge.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import config
from agent import audit_trail
from governance import github_gov, grafana_gov

_ICON = {"GREEN": "[GREEN]", "AMBER": "[AMBER]", "RED": "[RED]"}


def emit(tick: int, prediction: dict, decision: dict) -> None:
    level = decision["level"]
    print(
        f"{_ICON.get(level, '')} tick={tick} "
        f"health={prediction.get('pipeline_health')} "
        f"risk={prediction.get('risk_score')} "
        f"conf={prediction.get('confidence')} "
        f"lead={prediction.get('lead_time_minutes')}min "
        f"type={prediction.get('predicted_failure_type')} "
        f"[{prediction.get('source')}]"
    )
    if decision["should_alert"]:
        ev = "; ".join(prediction.get("evidence", [])[:4])
        print(f"    evidence : {ev}")
        print(f"    action   : {decision['recommendation']}")

    audit_trail.audit(
        "prediction",
        tick=tick,
        level=level,
        risk_score=prediction.get("risk_score"),
        pipeline_health=prediction.get("pipeline_health"),
        confidence=prediction.get("confidence"),
        lead_time_minutes=prediction.get("lead_time_minutes"),
        predicted_failure_type=prediction.get("predicted_failure_type"),
        source=prediction.get("source"),
    )

    issue_url = pr_url = None
    if decision["should_open_issue"]:
        issue_url = github_gov.open_predicted_incident_issue(prediction, decision)
    if decision["should_open_pr"]:
        pr_url = github_gov.open_preventive_pr(prediction, decision)

    if decision["should_alert"]:
        config.INCIDENTS_FILE.write_text(
            json.dumps(
                {
                    "level": level,
                    "prediction": prediction,
                    "issue_url": issue_url,
                    "pr_url": pr_url,
                    "opened": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                default=str,
            )
        )

        # Governor -> Grafana: drop an annotation on every dashboard at this exact
        # moment, and log the raised alert to the live stream (Promtail -> Loki)
        # so it also surfaces in the real-time Grafana log panels.
        try:
            annotation_id = grafana_gov.annotate(prediction, decision)
        except Exception:  # noqa: BLE001 - observability must never break the loop
            annotation_id = None
        audit_trail.stream_emit(
            "grafana_alert_raised",
            level=level,
            predicted_failure_type=prediction.get("predicted_failure_type"),
            risk_score=prediction.get("risk_score"),
            lead_time_minutes=prediction.get("lead_time_minutes"),
            annotation=annotation_id or "n/a",
        )

        # Governor -> Grafana IRM: declare the incident ONCE per predicted-failure
        # signature, carrying the AI's full analysis + remediation and links to the
        # governed issue / gated PR. De-duped, and auto-resolved on GREEN.
        try:
            incident_ref = grafana_gov.open_incident(
                prediction, decision,
                links={"issue_url": issue_url, "pr_url": pr_url},
            )
        except Exception:  # noqa: BLE001 - governance must never break the loop
            incident_ref = None
        audit_trail.stream_emit(
            "grafana_incident_declared",
            level=level,
            predicted_failure_type=prediction.get("predicted_failure_type"),
            risk_score=prediction.get("risk_score"),
            incident=incident_ref or "n/a",
        )


def clear_incident() -> None:
    config.INCIDENTS_FILE.write_text(json.dumps({"level": "GREEN"}, indent=2))
    try:
        grafana_gov.resolve_incidents()
    except Exception:  # noqa: BLE001 - governance must never break the loop
        pass
    audit_trail.audit("incident_cleared")
