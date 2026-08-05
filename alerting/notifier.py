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
import threading
from datetime import datetime, timezone

import config
from agent import audit_trail
from governance import github_gov, grafana_gov

_ICON = {"GREEN": "[GREEN]", "AMBER": "[AMBER]", "RED": "[RED]"}

# The last predicted-failure signature we opened governance for, so a climbing
# incident (many AMBER/RED ticks) governs ONCE, not every tick.
_last_gov_sig = None

REQUIRE_APPROVAL = config.GOVERNANCE_REQUIRE_APPROVAL


def _read_banner() -> dict:
    try:
        d = json.loads(config.INCIDENTS_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _latest_rca() -> dict | None:
    """Newest Opus-authored RCA from the stack (attached to the governed issue)."""
    try:
        d = json.loads((config.DATA_DIR / "rca_history.json").read_text(encoding="utf-8"))
        if isinstance(d, list) and d and isinstance(d[0], dict) and d[0].get("root_cause"):
            return d[0]
    except (OSError, json.JSONDecodeError):
        pass
    return None


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

    if decision["should_alert"]:
        # Write the banner IMMEDIATELY so the dashboard flips AMBER/RED with ZERO
        # lag. Preserve any approval already given for THIS incident across ticks
        # (emit runs every alert tick, so it must not reset the operator's approval).
        prev = _read_banner()
        same = (prev.get("level") in ("AMBER", "RED") and
                (prev.get("prediction") or {}).get("predicted_failure_type")
                == prediction.get("predicted_failure_type"))
        approved = bool(same and prev.get("approved"))
        config.INCIDENTS_FILE.write_text(
            json.dumps(
                {
                    "level": level,
                    "prediction": prediction,
                    "require_approval": REQUIRE_APPROVAL,
                    "awaiting_approval": (REQUIRE_APPROVAL and not approved),
                    "approved": approved,
                    "issue_url": prev.get("issue_url") if same else None,
                    "pr_url": prev.get("pr_url") if same else None,
                    "opened": prev.get("opened") if same else datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
                default=str,
            )
        )
        global _last_gov_sig
        sig = github_gov.signature(prediction)
        if sig != _last_gov_sig:
            _last_gov_sig = sig
            # The Grafana observability MARKER is monitoring (not a governed change),
            # so it is always dropped. The GitHub issue / preventive PR / IRM incident
            # are GOVERNED actions: auto-filed only when approval is NOT required;
            # otherwise they wait for the operator to click Approve on the dashboard.
            threading.Thread(target=_annotate_async,
                             args=(prediction, decision, level), daemon=True).start()
            if not REQUIRE_APPROVAL:
                threading.Thread(target=_govern_async,
                                 args=(prediction, decision, level, _latest_rca()),
                                 daemon=True).start()


def _annotate_async(prediction: dict, decision: dict, level: str) -> None:
    """Drop the Grafana observability marker + live-stream log (monitoring only —
    this is NOT a governed change, so it never needs approval)."""
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


def _govern_async(prediction: dict, decision: dict, level: str,
                  rca: dict | None = None) -> tuple:
    """File the GOVERNED artifacts: the AI-written GitHub issue / gated PR and the
    Grafana IRM incident, then patch the banner with the links. Returns
    ``(issue_url, pr_url)``. Never auto-merges. Runs once per incident / on approval."""
    issue_url = pr_url = None
    try:
        if decision.get("should_open_issue"):
            issue_url = github_gov.open_predicted_incident_issue(prediction, decision, rca=rca)
        if decision.get("should_open_pr"):
            pr_url = github_gov.open_preventive_pr(prediction, decision, rca=rca)
    except Exception:  # noqa: BLE001 - governance must never break anything
        pass

    # Patch the banner with the links, but only if this incident is still showing.
    try:
        cur = _read_banner()
        if cur.get("level") == level:
            cur["issue_url"] = issue_url
            cur["pr_url"] = pr_url
            config.INCIDENTS_FILE.write_text(json.dumps(cur, indent=2, default=str))
    except (OSError, json.JSONDecodeError):
        pass

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
    return issue_url, pr_url


def _grounded_rca(prediction: dict) -> dict:
    """Instant grounded RCA (no model call) so an approved issue ALWAYS carries the
    AI analysis + steps, even if the Opus RCA has not finished generating yet."""
    try:
        sig = json.loads((config.DATA_DIR / "signal_history.json").read_text(encoding="utf-8"))
        sig = sig if isinstance(sig, list) else []
    except (OSError, json.JSONDecodeError):
        sig = []
    from agent import rca as rca_mod
    return rca_mod.generate_rca(prediction, sig, None)


def approve_active_incident() -> dict:
    """Operator approval from the dashboard: file the governed GitHub issue / gated
    PR (AI-written) + Grafana IRM incident for the ACTIVE incident. Nothing is filed
    before this click, and nothing is ever auto-merged."""
    banner = _read_banner()
    level = banner.get("level")
    if level not in ("AMBER", "RED"):
        return {"status": "no_active_incident"}
    if banner.get("approved"):
        return {"status": "already_approved",
                "issue_url": banner.get("issue_url"), "pr_url": banner.get("pr_url")}
    prediction = banner.get("prediction") or {}
    # Attach the AI analysis for THIS incident. The loop's newest RCA can still be
    # for a PREVIOUS incident (the new one's Opus RCA may not have generated yet),
    # so only use it when its signature matches; otherwise generate a grounded RCA
    # for the current prediction NOW (instant) so the issue/PR are never stale or
    # a bare template.
    cur_sig = github_gov.signature(prediction)
    rca = _latest_rca()
    if not (rca and rca.get("signature") == cur_sig):
        rca = _grounded_rca(prediction)
    # Raise a PR ONLY when a code/logic change is needed; a purely operational
    # (manual) fix gets the step-by-step guidance in the issue, with no PR.
    fix_type = str((rca or {}).get("fix_type") or "").lower()
    decision = {
        "level": level,
        "should_alert": True,
        "should_open_issue": True,
        "should_open_pr": (fix_type == "code"),
        "recommendation": prediction.get("recommended_action", ""),
    }
    issue_url, pr_url = _govern_async(prediction, decision, level, rca)
    banner = _read_banner()
    banner["approved"] = True
    banner["awaiting_approval"] = False
    banner["issue_url"] = issue_url
    banner["pr_url"] = pr_url
    banner["approved_at"] = datetime.now(timezone.utc).isoformat()
    try:
        config.INCIDENTS_FILE.write_text(json.dumps(banner, indent=2, default=str))
    except OSError:
        pass
    audit_trail.audit("governance_approved", level=level,
                      predicted_failure_type=prediction.get("predicted_failure_type"),
                      issue_url=issue_url, pr_url=pr_url)
    return {"status": "approved", "issue_url": issue_url, "pr_url": pr_url}


def clear_incident() -> None:
    global _last_gov_sig
    _last_gov_sig = None
    config.INCIDENTS_FILE.write_text(json.dumps({"level": "GREEN"}, indent=2))
    try:
        grafana_gov.resolve_incidents()
    except Exception:  # noqa: BLE001 - governance must never break the loop
        pass
    audit_trail.audit("incident_cleared")
