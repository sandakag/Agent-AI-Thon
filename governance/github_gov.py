"""REAL GitHub governance — predicted-incident issue + gated preventive PR.

The guardian does all the on-call + developer legwork the moment it *predicts* a
failure — it files the ticket, writes a preventive runbook, and opens a pull
request — then **stops**. A human reviews and merges; nothing is ever
auto-merged. Everything de-duplicates per predicted-failure signature, so a
climbing risk score (AMBER → RED over several ticks) never spams the repo.

Implemented against the GitHub REST API with the standard-library ``urllib``
(no extra dependencies). Requires two env values (see ``.env``):

    GITHUB_TOKEN       a PAT with ``repo`` scope (issues + contents + pull requests)
    GITHUB_REPOSITORY  ``owner/repo``

If either is missing the functions degrade to a printed "would open …" plan and
record the intent in the audit trail — so a demo without credentials is safe and
still tells the full story.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import config
from agent import audit_trail

_API = "https://api.github.com"
_MARK = "predictive-guardian"
_LABEL = "predicted-incident"


def enabled() -> bool:
    return bool(config.GITHUB_TOKEN and config.GITHUB_REPOSITORY)


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list]:
    url = path if path.startswith("http") else f"{_API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {config.GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "PredictivePipelineGuardian")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, {"error": str(exc)}


def signature(prediction: dict) -> str:
    """Stable per-failure key used to de-duplicate issues and PR branches."""
    ft = str(prediction.get("predicted_failure_type") or "unknown").strip().lower()
    return "".join(c if c.isalnum() else "-" for c in ft).strip("-") or "unknown"


def _marker(sig: str) -> str:
    return f"<!-- {_MARK}:{sig} -->"


# ---------------------------------------------------------------------------
# Predicted-incident ISSUE (the early ticket)
# ---------------------------------------------------------------------------
def _ensure_label() -> None:
    repo = config.GITHUB_REPOSITORY
    status, _ = _req("GET", f"/repos/{repo}/labels/{_LABEL}")
    if status == 404:
        _req(
            "POST",
            f"/repos/{repo}/labels",
            {"name": _LABEL, "color": "b60205",
             "description": "Failure predicted BEFORE it happened by the AI guardian"},
        )


def find_open_issue(sig: str) -> tuple[str | None, int | None]:
    repo = config.GITHUB_REPOSITORY
    status, items = _req(
        "GET", f"/repos/{repo}/issues?state=open&labels={_LABEL}&per_page=100"
    )
    if status == 200 and isinstance(items, list):
        marker = _marker(sig)
        for it in items:
            if marker in (it.get("body") or ""):
                return it.get("html_url"), it.get("number")
    return None, None


def _issue_body(prediction: dict, decision: dict, sig: str, rca: dict | None = None) -> str:
    header = (
        f"{_marker(sig)}\n"
        f"## 🔮 Predicted pipeline incident — raised BEFORE failure\n\n"
        f"| Signal | Value |\n|---|---|\n"
        f"| Severity | **{decision.get('level')}** |\n"
        f"| Risk score | **{prediction.get('risk_score')}/100** |\n"
        f"| Predicted lead time | ~{prediction.get('lead_time_minutes')} min |\n"
        f"| Confidence | {prediction.get('confidence')} |\n"
        f"| Reasoned by | `{prediction.get('source')}` |\n\n"
    )
    footer = (
        "\n> 🛡️ Governance: the AI predicted this and WROTE the analysis above, then "
        "STOPPED. A human approved filing this ticket; a human approves any fix or merge.\n"
    )
    # Prefer the AI-written (Opus) root-cause analysis — the SAME analysis shown on
    # the dashboard — so the issue is genuinely authored, not a static template.
    if rca and rca.get("root_cause"):
        try:
            from agent import rca as rca_mod
            return header + rca_mod.render_markdown(rca) + footer
        except Exception:  # noqa: BLE001
            pass
    ev = "\n".join(f"- {e}" for e in prediction.get("evidence", [])[:8]) or "- (none)"
    return (
        header
        + f"### Grounded evidence\n{ev}\n\n"
        + f"### Recommended preventive action\n{prediction.get('recommended_action')}\n"
        + footer
    )


def open_predicted_incident_issue(prediction: dict, decision: dict,
                                  rca: dict | None = None) -> str | None:
    """Open (or reuse) the predicted-incident issue. De-dupes per signature."""
    sig = signature(prediction)
    if not enabled():
        audit_trail.audit("governance_issue_planned", signature=sig,
                          severity=decision.get("level"), enabled=False)
        print("    -> [governance] would open GitHub PREDICTED-INCIDENT issue "
              "(set GITHUB_TOKEN + GITHUB_REPOSITORY to enable)")
        return None

    existing_url, existing_num = find_open_issue(sig)
    if existing_url:
        # Keep a REUSED ticket fresh: overwrite its body with the latest AI-written
        # RCA so it is never a stale, repeated template (the de-dupe used to return
        # the old body verbatim, which is why every issue looked identical).
        if rca and rca.get("root_cause") and existing_num:
            _req("PATCH", f"/repos/{config.GITHUB_REPOSITORY}/issues/{existing_num}",
                 {"body": _issue_body(prediction, decision, sig, rca)})
            audit_trail.audit("governance_issue_refreshed", signature=sig, url=existing_url)
            print(f"    -> [governance] predicted-incident issue refreshed (AI-written): {existing_url}")
        else:
            audit_trail.audit("governance_issue_deduped", signature=sig, url=existing_url)
            print(f"    -> [governance] predicted-incident issue already open: {existing_url}")
        return existing_url

    _ensure_label()
    repo = config.GITHUB_REPOSITORY
    title = (f"[{decision.get('level')}] Predicted: "
             f"{prediction.get('predicted_failure_type')} "
             f"(risk {prediction.get('risk_score')}/100)")
    status, data = _req(
        "POST", f"/repos/{repo}/issues",
        {"title": title, "body": _issue_body(prediction, decision, sig, rca),
         "labels": [_LABEL]},
    )
    if status in (200, 201) and isinstance(data, dict):
        url = data.get("html_url")
        audit_trail.audit("governance_issue_opened", signature=sig,
                          number=data.get("number"), url=url,
                          severity=decision.get("level"))
        print(f"    -> [governance] predicted-incident ISSUE opened: {url}")
        return url
    audit_trail.audit("governance_issue_failed", signature=sig, status=status,
                     detail=str(data)[:200])
    print(f"    -> [governance] issue API error ({status}): {str(data)[:120]}")
    return None


# ---------------------------------------------------------------------------
# Gated preventive PULL REQUEST (the staged fix — never auto-merged)
# ---------------------------------------------------------------------------
def _default_branch() -> str | None:
    status, data = _req("GET", f"/repos/{config.GITHUB_REPOSITORY}")
    if status == 200 and isinstance(data, dict):
        return data.get("default_branch")
    return None


def find_open_pr(branch: str) -> str | None:
    repo = config.GITHUB_REPOSITORY
    owner = repo.split("/")[0]
    status, items = _req(
        "GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}"
    )
    if status == 200 and isinstance(items, list) and items:
        return items[0].get("html_url")
    return None


def _runbook(prediction: dict, decision: dict, sig: str, rca: dict | None = None) -> str:
    now = datetime.now(timezone.utc).isoformat()
    head = (
        f"# Preventive remediation — {prediction.get('predicted_failure_type')}\n\n"
        f"_Staged by the Predictive Pipeline Guardian at {now}. Gated — a human "
        f"approves the merge._\n\n"
    )
    if rca and rca.get("root_cause"):
        try:
            from agent import rca as rca_mod
            return head + rca_mod.render_markdown(rca)
        except Exception:  # noqa: BLE001
            pass
    ev = "\n".join(f"- {e}" for e in prediction.get("evidence", [])[:8]) or "- (none)"
    return (
        head
        + f"- **Severity:** {decision.get('level')}\n"
        + f"- **Risk score:** {prediction.get('risk_score')}/100\n"
        + f"- **Predicted lead time:** ~{prediction.get('lead_time_minutes')} min\n\n"
        + f"## Evidence\n{ev}\n\n## Recommended action\n{prediction.get('recommended_action')}\n"
    )


def open_preventive_pr(prediction: dict, decision: dict,
                       rca: dict | None = None) -> str | None:
    """Open a gated preventive PR that commits a remediation runbook. De-dupes
    per signature; NEVER auto-merged."""
    sig = signature(prediction)
    branch = f"guardian/prevent-{sig}"
    if not enabled():
        audit_trail.audit("governance_pr_planned", signature=sig, enabled=False)
        print("    -> [governance] would open GATED preventive PR "
              f"({prediction.get('recommended_action', '')[:60]}...) — human approves")
        return None

    existing = find_open_pr(branch)
    if existing:
        audit_trail.audit("governance_pr_deduped", signature=sig, url=existing)
        print(f"    -> [governance] gated preventive PR already open: {existing}")
        return existing

    repo = config.GITHUB_REPOSITORY
    base = _default_branch()
    if not base:
        audit_trail.audit("governance_pr_failed", signature=sig, reason="no_default_branch")
        return None

    # 1) base ref sha
    status, ref = _req("GET", f"/repos/{repo}/git/ref/heads/{base}")
    if status != 200 or not isinstance(ref, dict):
        audit_trail.audit("governance_pr_failed", signature=sig, reason="no_base_ref",
                         status=status)
        return None
    base_sha = ref["object"]["sha"]

    # 2) create the branch (idempotent — 422 if it already exists)
    _req("POST", f"/repos/{repo}/git/refs",
         {"ref": f"refs/heads/{branch}", "sha": base_sha})

    # 3) commit the preventive runbook on the branch
    import base64

    path = f"prevention/{sig}.md"
    content_b64 = base64.b64encode(
        _runbook(prediction, decision, sig, rca).encode("utf-8")
    ).decode("ascii")
    # if the file already exists on the branch we need its blob sha to update
    sha = None
    st, existing_file = _req("GET", f"/repos/{repo}/contents/{path}?ref={branch}")
    if st == 200 and isinstance(existing_file, dict):
        sha = existing_file.get("sha")
    put_body = {
        "message": f"guardian: stage preventive fix for {prediction.get('predicted_failure_type')}",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        put_body["sha"] = sha
    st, _ = _req("PUT", f"/repos/{repo}/contents/{path}", put_body)
    if st not in (200, 201):
        audit_trail.audit("governance_pr_failed", signature=sig, reason="commit_failed",
                         status=st)
        return None

    # 4) open the PR (gated — never merged here)
    issue_url, _num = find_open_issue(sig)
    body = (
        f"Preventive fix staged by the **Predictive Pipeline Guardian** for a "
        f"forecast **{prediction.get('predicted_failure_type')}** failure "
        f"(risk {prediction.get('risk_score')}/100, ~"
        f"{prediction.get('lead_time_minutes')} min lead time).\n\n"
        f"**Recommended action:** {prediction.get('recommended_action')}\n\n"
        f"{'Related issue: ' + issue_url if issue_url else ''}\n\n"
        f"> 🛡️ Gated: review and merge to promote. The AI never auto-merges."
    )
    st, pr = _req("POST", f"/repos/{repo}/pulls",
                  {"title": f"[preventive] {prediction.get('predicted_failure_type')}",
                   "head": branch, "base": base, "body": body})
    if st in (200, 201) and isinstance(pr, dict):
        url = pr.get("html_url")
        audit_trail.audit("governance_pr_opened", signature=sig,
                         number=pr.get("number"), url=url)
        print(f"    -> [governance] GATED preventive PR opened: {url}")
        return url
    audit_trail.audit("governance_pr_failed", signature=sig, reason="pr_api",
                     status=st, detail=str(pr)[:200])
    print(f"    -> [governance] PR API error ({st}): {str(pr)[:120]}")
    return None
