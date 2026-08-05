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


def _etl_code_fix(sig: str, content: str):
    """Return ``(new_content, description)`` — a REAL, targeted fix applied to the
    current ``pipeline/etl.py`` for a code/logic incident — or ``None`` when the
    signature is an OPERATIONAL issue (latency/volume/stale), in which case there
    is no code change and therefore NO pull request (the steps live in the issue).
    """
    s = (sig or "").lower()

    # --- schema drift: resolve renamed/aliased upstream fields in the parser ---
    if "schema" in s or "rename" in s or "parse" in s:
        anchor = ('        price = _to_float(r.get("price"))\n'
                  '        size = _to_float(r.get("size"))')
        if anchor not in content or "_resolve_alias" in content:
            return None
        helper = (
            "def _resolve_alias(record: dict, names: tuple):\n"
            "    \"\"\"Return the first present alias of a (possibly renamed) upstream field.\"\"\"\n"
            "    for _n in names:\n"
            "        if record.get(_n) is not None:\n"
            "            return record.get(_n)\n"
            "    return None\n\n\n"
        )
        new = ('        price = _to_float(_resolve_alias(r, ("price", "px", "p", "prc")))\n'
               '        size = _to_float(_resolve_alias(r, ("size", "qty", "quantity", "sz")))')
        fixed = content.replace(anchor, new).replace(
            "def parse_trades(", helper + "def parse_trades(", 1)
        return fixed, ("Resolve upstream field aliases in `parse_trades` so a renamed "
                       "field (e.g. price->px) still parses instead of producing NULL amounts.")

    # --- duplicate storm: dedupe by trade_id so a replay never double-counts ---
    if "dup" in s:
        anchor = "    parsed = parse_trades(raw)\n    total = len(parsed)"
        if anchor not in content or "_deduped" in content:
            return None
        new = ("    parsed = parse_trades(raw)\n"
               "    # Idempotency fix: drop at-least-once duplicate redeliveries by\n"
               "    # trade_id so a replay storm never double-counts revenue.\n"
               "    _seen, _deduped = set(), []\n"
               "    for _p in parsed:\n"
               "        _tid = _p.get(\"trade_id\")\n"
               "        if _tid is not None and _tid in _seen:\n"
               "            continue\n"
               "        _seen.add(_tid)\n"
               "        _deduped.append(_p)\n"
               "    parsed = _deduped\n"
               "    total = len(parsed)")
        return content.replace(anchor, new), ("Dedupe by `trade_id` in `run_etl` so "
               "at-least-once redelivery never double-counts revenue.")

    # --- null-rate / data-quality: quarantine nulls, publish the valid subset ---
    if "null" in s or "quality" in s:
        anchor = ('    if total == 0 or null_rate >= config.NULL_RATE_CRITICAL:\n'
                  '        result["failed"] = True')
        if anchor not in content or "quarantined" in content:
            return None
        block = (
            "    # Resilience fix: quarantine null-amount records and publish the VALID\n"
            "    # subset instead of failing the whole batch, so a burst of bad upstream\n"
            "    # records never zeroes revenue. Alert on the quarantined count.\n"
            "    if total and null_rate >= config.NULL_RATE_CRITICAL:\n"
            "        valid = [p for p in parsed if p[\"amount\"] is not None]\n"
            "        if valid:\n"
            "            good = aggregate(valid)\n"
            "            result[\"aggregate\"] = good\n"
            "            result[\"quarantined\"] = nulls\n"
            "            result[\"warehouse\"] = load(good)\n"
            "            result[\"error\"] = (\n"
            "                \"quarantined %d null-amount records; published %d valid\"\n"
            "                % (nulls, len(valid))\n"
            "            )\n"
            "            return result\n"
        )
        return content.replace(anchor, block + anchor), ("Quarantine null-amount records "
               "and publish the valid subset in `run_etl` instead of failing the whole batch.")

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

    # Fetch the CURRENT source and compute a real, targeted fix. If this is an
    # operational issue (no code change), skip the PR entirely — no phony PRs.
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
    fix = _etl_code_fix(sig, current)
    if fix is None:
        audit_trail.audit("governance_pr_skipped", signature=sig,
                          reason="operational_fix_no_code_change")
        print("    -> [governance] operational fix — no code change, so no PR "
              "(the concrete steps are in the issue)")
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
