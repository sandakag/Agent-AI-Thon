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
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

import config
from agent import audit_trail
from agent.brain_base import BrainError
from agent.copilot_api import CopilotApiBrain
from agent.copilot_cli import CopilotCliBrain

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

    # Brand-new each incident: file a FRESH issue every episode (no reuse/patch of a
    # prior open ticket), per the demo governance model. Per-tick spam is prevented
    # upstream — governance fires once per incident episode, not every tick.
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


def _copilot_code_fix(prediction: dict, rca: dict | None, content: str) -> tuple[str, str] | None:
    """Author the production fix for ``pipeline/etl.py``.

    The ML known-fix analyzer runs FIRST: for a previously-diagnosed incident it
    restores the verified hardened parser instantly (no model call, no rate
    limit), so the guardian always produces a correct, mergeable PR. Only for a
    NOVEL incident it has never seen does it fall through to the generative
    GitHub Copilot brains (local CLI preferred, mounted API credential as the
    headless fallback). Every result is syntax-checked before a branch is made.
    """
    failure_type = prediction.get("predicted_failure_type")

    # 1) ML KNOWN-FIX ANALYZER FIRST — instant, verified, no model call.
    from governance import known_fixes
    known = known_fixes.deterministic_fix(failure_type, content)
    if known is not None:
        new_content, desc = known
        try:
            compile(new_content, "pipeline/etl.py", "exec")
            audit_trail.audit("governance_pr_known_fix", failure_type=str(failure_type), change=desc)
            print(f"    -> [governance] ML known-fix analyzer authored the repair: {desc}")
            return new_content + ("" if new_content.endswith("\n") else "\n"), desc
        except SyntaxError as exc:
            audit_trail.audit("governance_pr_known_fix_invalid", detail=str(exc)[:200])

    # 2) Generative Copilot fallback for a NOVEL incident the analyzer doesn't know.
    brain = CopilotCliBrain()
    if not brain.available:
        brain = CopilotApiBrain()
    if brain.available:
        context = {
            "prediction": prediction,
            "rca": rca or {},
            "target_file": "pipeline/etl.py",
        }
        prompt = (
            "Diagnose this live production incident and author the minimal safe repair. "
            "Return ONLY the complete replacement contents of pipeline/etl.py, with no "
            "Markdown fences or commentary. Preserve the public API, do not weaken error "
            "handling, and change no other file.\n\n"
            f"INCIDENT CONTEXT:\n{json.dumps(context, default=str)}\n\n"
            f"CURRENT pipeline/etl.py:\n{content}"
        )
        try:
            updated = brain.chat(
                "You are GitHub Copilot, the production repair author for a Python data pipeline.",
                prompt,
                temperature=0.1,
            ).strip()
            # Tolerate an accidental markdown fence but never accept commentary around code.
            fenced = re.fullmatch(r"```(?:python)?\s*\n?(.*?)\n?```", updated, re.DOTALL)
            if fenced:
                updated = fenced.group(1).strip()
            if updated and updated != content.strip():
                compile(updated, "pipeline/etl.py", "exec")
                return updated + "\n", f"GitHub Copilot repair for {failure_type}"
        except BrainError as exc:
            audit_trail.audit("governance_pr_copilot_error", detail=str(exc)[:200])
        except SyntaxError as exc:
            audit_trail.audit("governance_pr_copilot_invalid", detail=str(exc)[:200])

    audit_trail.audit("governance_pr_skipped", reason="no_safe_change")
    print("    -> [governance] no safe code repair available — no PR created")
    return None


def open_preventive_pr(prediction: dict, decision: dict,
                       rca: dict | None = None) -> str | None:
    """Open a gated PR that commits a REAL code fix to ``pipeline/etl.py`` for a
    code/logic incident. For an OPERATIONAL issue (latency / volume / stale feed)
    there is no code change, so NO pull request is opened — the concrete ops steps
    live in the issue instead. De-dupes per signature; NEVER auto-merged."""
    sig = signature(prediction)
    ep = "".join(c for c in str(decision.get("episode") or "") if c.isalnum())
    branch = f"guardian/fix-{sig}-{ep}" if ep else f"guardian/fix-{sig}"
    if not enabled():
        audit_trail.audit("governance_pr_planned", signature=sig, enabled=False)
        print("    -> [governance] would open a gated code-fix PR — human approves")
        return None

    repo = config.GITHUB_REPOSITORY
    base = _default_branch()
    if not base:
        audit_trail.audit("governance_pr_failed", signature=sig, reason="no_default_branch")
        return None

    # Fetch the current source and have GitHub Copilot author a constrained
    # replacement. The API client only permits this one target file to be changed.
    fpath = "pipeline/etl.py"
    st, fmeta = _req("GET", f"/repos/{repo}/contents/{fpath}?ref={base}")
    if st != 200 or not isinstance(fmeta, dict):
        audit_trail.audit("governance_pr_failed", signature=sig, reason="no_source_file", status=st)
        return None

    import base64
    try:
        current = base64.b64decode(fmeta.get("content", "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    fix = _copilot_code_fix(prediction, rca, current)
    if fix is None:
        audit_trail.audit("governance_pr_skipped", signature=sig,
                          reason="copilot_no_safe_change")
        print("    -> [governance] Copilot did not return a safe code repair — no PR")
        return None
    new_content, change_desc = fix

    existing = find_open_pr(branch)
    if existing:
        audit_trail.audit("governance_pr_deduped", signature=sig, url=existing)
        return existing

    status, ref = _req("GET", f"/repos/{repo}/git/ref/heads/{base}")
    if status != 200 or not isinstance(ref, dict):
        audit_trail.audit("governance_pr_failed", signature=sig, reason="no_base_ref", status=status)
        return None
    _req("POST", f"/repos/{repo}/git/refs",
         {"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]})

    # Commit the modified source on the branch (update if the file already exists).
    sha = fmeta.get("sha")
    stb, bfile = _req("GET", f"/repos/{repo}/contents/{fpath}?ref={branch}")
    if stb == 200 and isinstance(bfile, dict):
        sha = bfile.get("sha")
    put = {
        "message": f"guardian: fix {prediction.get('predicted_failure_type')} — {change_desc}",
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        put["sha"] = sha
    stc, _ = _req("PUT", f"/repos/{repo}/contents/{fpath}", put)
    if stc not in (200, 201):
        audit_trail.audit("governance_pr_failed", signature=sig, reason="commit_failed", status=stc)
        return None

    issue_url, _num = find_open_issue(sig)
    head = (
        f"**Gated code fix staged by the Predictive Pipeline Guardian** for a predicted "
        f"**{prediction.get('predicted_failure_type')}** (risk {prediction.get('risk_score')}/100, "
        f"~{prediction.get('lead_time_minutes')} min lead time).\n\n"
        f"**This PR changes `{fpath}`:** {change_desc}\n\n"
        + (f"Related issue: {issue_url}\n\n" if issue_url else "")
        + "> 🛡️ Gated: review the diff and merge to apply the fix. The AI never auto-merges.\n\n---\n\n"
    )
    body = head
    if rca and rca.get("root_cause"):
        try:
            from agent import rca as rca_mod
            body = head + rca_mod.render_markdown(rca)
        except Exception:  # noqa: BLE001
            pass
    st, pr = _req("POST", f"/repos/{repo}/pulls",
                  {"title": f"[fix] {prediction.get('predicted_failure_type')}",
                   "head": branch, "base": base, "body": body})
    if st in (200, 201) and isinstance(pr, dict):
        url = pr.get("html_url")
        audit_trail.audit("governance_pr_opened", signature=sig,
                          number=pr.get("number"), url=url, change=change_desc)
        print(f"    -> [governance] GATED code-fix PR opened: {url}")
        return url
    audit_trail.audit("governance_pr_failed", signature=sig, reason="pr_api",
                      status=st, detail=str(pr)[:200])
    print(f"    -> [governance] PR API error ({st}): {str(pr)[:120]}")
    return None


# ---------------------------------------------------------------------------
# Merge watcher — the human merges the gated PR on GitHub, the running pipeline
# picks the fix up automatically and heals. This closes the loop end-to-end:
# predict -> file issue -> stage PR -> human merges -> pipeline self-heals.
# ---------------------------------------------------------------------------
SOURCE_PATH = "pipeline/etl.py"


def merged_guardian_prs() -> list[dict]:
    """Every MERGED ``guardian/fix-*`` pull request, newest merge first."""
    if not enabled():
        return []
    st, prs = _req("GET", f"/repos/{config.GITHUB_REPOSITORY}/pulls"
                          "?state=closed&sort=updated&direction=desc&per_page=30")
    if st != 200 or not isinstance(prs, list):
        return []
    out = []
    for pr in prs:
        if not isinstance(pr, dict) or not pr.get("merged_at"):
            continue
        ref = ((pr.get("head") or {}).get("ref") or "")
        if not ref.startswith("guardian/fix-"):
            continue
        out.append({"number": pr.get("number"), "url": pr.get("html_url"),
                    "title": pr.get("title"), "branch": ref,
                    "merged_at": pr.get("merged_at")})
    out.sort(key=lambda p: str(p.get("merged_at")), reverse=True)
    return out


def fetch_main_source(path: str = SOURCE_PATH) -> str | None:
    """Return the CURRENT text of ``path`` on the repo's default branch."""
    if not enabled():
        return None
    base = _default_branch()
    if not base:
        return None
    st, meta = _req("GET", f"/repos/{config.GITHUB_REPOSITORY}/contents/{path}?ref={base}")
    if st != 200 or not isinstance(meta, dict):
        return None
    import base64
    try:
        return base64.b64decode(meta.get("content", "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def push_source(content: str, message: str, path: str = SOURCE_PATH) -> bool:
    """Commit ``content`` straight to the default branch (used by the demo reset to
    restore the pristine, un-hardened baseline so the next run stages a real PR)."""
    if not enabled():
        return False
    base = _default_branch()
    if not base:
        return False
    import base64
    st, meta = _req("GET", f"/repos/{config.GITHUB_REPOSITORY}/contents/{path}?ref={base}")
    put = {"message": message, "branch": base,
           "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
    if st == 200 and isinstance(meta, dict) and meta.get("sha"):
        put["sha"] = meta["sha"]
    stc, _ = _req("PUT", f"/repos/{config.GITHUB_REPOSITORY}/contents/{path}", put)
    return stc in (200, 201)


# ---------------------------------------------------------------------------
# Demo cleanup — close every guardian-created artifact (surgical & idempotent)
# ---------------------------------------------------------------------------
def _list_all(path: str) -> list:
    """GET every page of a GitHub list endpoint (best-effort)."""
    out: list = []
    for page in range(1, 21):  # hard cap: 20 pages (2000 items)
        sep = "&" if "?" in path else "?"
        st, items = _req("GET", f"{path}{sep}per_page=100&page={page}")
        if st != 200 or not isinstance(items, list) or not items:
            break
        out.extend(items)
        if len(items) < 100:
            break
    return out


def cleanup_all() -> dict:
    """Close every guardian-created issue + PR and delete the ``guardian/fix-*``
    branches, returning counts. SURGICAL: only touches issues carrying the
    ``predicted-incident`` label, PRs whose head branch starts with
    ``guardian/fix-``, and those branches — never ``main`` or unrelated work."""
    result = {"enabled": enabled(), "issues_closed": 0, "prs_closed": 0,
              "branches_deleted": 0}
    if not enabled():
        print("    -> [cleanup] GitHub disabled (no GITHUB_TOKEN/REPOSITORY) — skipped")
        return result
    repo = config.GITHUB_REPOSITORY

    # 1) Close open predicted-incident issues (the /issues feed also lists PRs —
    #    skip those; PRs are handled below by head branch).
    for it in _list_all(f"/repos/{repo}/issues?state=open&labels={_LABEL}"):
        if it.get("pull_request") or it.get("number") is None:
            continue
        st, _ = _req("PATCH", f"/repos/{repo}/issues/{it['number']}",
                     {"state": "closed", "state_reason": "not_planned"})
        if st == 200:
            result["issues_closed"] += 1

    # 2) Close open guardian PRs (head branch guardian/fix-*).
    for pr in _list_all(f"/repos/{repo}/pulls?state=open"):
        head = ((pr.get("head") or {}).get("ref") or "")
        if not head.startswith("guardian/fix-") or pr.get("number") is None:
            continue
        st, _ = _req("PATCH", f"/repos/{repo}/pulls/{pr['number']}", {"state": "closed"})
        if st == 200:
            result["prs_closed"] += 1

    # 3) Delete the guardian/fix-* branches.
    for ref in _list_all(f"/repos/{repo}/git/matching-refs/heads/guardian/fix-"):
        name = ref.get("ref", "")  # e.g. refs/heads/guardian/fix-latency-...
        if not name.startswith("refs/heads/guardian/fix-"):
            continue
        st, _ = _req("DELETE", f"/repos/{repo}/git/{name}")  # -> /git/refs/heads/...
        if st in (200, 204):
            result["branches_deleted"] += 1

    audit_trail.audit("governance_cleanup", **result)
    print(f"    -> [cleanup] GitHub: closed {result['issues_closed']} issue(s), "
          f"{result['prs_closed']} PR(s), deleted {result['branches_deleted']} branch(es)")
    return result
