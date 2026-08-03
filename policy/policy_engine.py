"""Preventive policy engine — turn a prediction into a graded decision plus
recommended actions.

    GREEN  risk < RISK_AMBER            -> just keep watching
    AMBER  RISK_AMBER <= risk < RISK_RED -> EARLY WARNING (alert + predicted
                                            incident ticket) BEFORE any failure
    RED    risk >= RISK_RED             -> imminent: also stage a gated
                                            preventive PR for a human to approve
"""

from __future__ import annotations

import config


def decide(prediction: dict) -> dict:
    risk = prediction.get("risk_score", 0)
    if risk >= config.RISK_RED:
        level = "RED"
    elif risk >= config.RISK_AMBER:
        level = "AMBER"
    else:
        level = "GREEN"

    return {
        "level": level,
        "should_alert": level in ("AMBER", "RED"),
        "should_open_issue": level in ("AMBER", "RED"),  # predicted-incident ticket, early
        "should_open_pr": level == "RED",                # gated preventive fix
        "recommendation": prediction.get("recommended_action", ""),
    }
