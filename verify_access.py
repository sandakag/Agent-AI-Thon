"""One-shot capability self-test for the Predictive Pipeline Guardian.

Verifies, using the token(s) in .env, that everything the project needs works:
  1. GitHub Models  — the agent's reasoning brain (a real chat call)
  2. GitHub API      — token identity + scopes
  3. Repo access     — can we read the target repo and do we have push rights
  4. Push (write)    — create a throwaway ref and delete it (fully reversible)

Secrets are NEVER printed — tokens are masked to <prefix>...<last4>.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import config
from agent.github_models import GitHubModels, GitHubModelsError

GH_API = "https://api.github.com"


def mask(tok: str) -> str:
    if not tok:
        return "(none)"
    return f"{tok[:10]}...{tok[-4:]} (len={len(tok)})"


def api(method: str, path: str, token: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GH_API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ppg-selftest",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}, dict(e.headers or {})
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, {"error": str(e)}, {}


def repo_slug() -> str:
    if config.GITHUB_REPOSITORY:
        return config.GITHUB_REPOSITORY
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], text=True
        ).strip()
        return url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
    except Exception:
        return ""


def main() -> None:
    print("=" * 76)
    print("Predictive Pipeline Guardian - capability self-test")
    print("=" * 76)

    models_token = os.environ.get("GITHUB_MODELS_TOKEN", "")
    api_token = os.environ.get("GITHUB_TOKEN", "") or models_token
    slug = repo_slug()
    reused = "  (reusing MODELS token)" if not os.environ.get("GITHUB_TOKEN") else ""
    print(f"GITHUB_MODELS_TOKEN : {mask(models_token)}")
    print(f"git/API token       : {mask(api_token)}{reused}")
    print(f"target repo         : {slug or '(unknown)'}")
    print(f"model               : {config.GITHUB_MODEL}")
    print("-" * 76)

    results: dict[str, bool] = {}

    # 1) GitHub Models — the agent brain
    print("[1] GitHub Models (agent brain) ...")
    try:
        gm = GitHubModels()
        out = gm.chat_json(
            "You are a test. Reply ONLY strict JSON.",
            'Return {"ok": true, "pong": "predictive-pipeline"} exactly.',
        )
        ok = bool(out) and out.get("ok") is True
        print(f"    -> {'PASS' if ok else 'PARTIAL'} model replied: {out}")
        results["models"] = ok
    except GitHubModelsError as e:
        print(f"    -> FAIL {e}")
        results["models"] = False

    if not api_token:
        print("[2-4] skipped - no GitHub API token available.")
        return _summary(results)

    # 2) API identity
    print("[2] GitHub API identity (/user) ...")
    st, body, hdr = api("GET", "/user", api_token)
    if st == 200:
        scopes = hdr.get("X-OAuth-Scopes", "(fine-grained PAT - per-repo perms)")
        print(f"    -> PASS authenticated as '{body.get('login')}'  scopes: {scopes}")
        results["identity"] = True
    else:
        print(f"    -> FAIL HTTP {st}: {body.get('error')}")
        results["identity"] = False

    # 3) Repo read + push permission
    push_ok = False
    if slug:
        print(f"[3] Repo access ({slug}) ...")
        st, body, _ = api("GET", f"/repos/{slug}", api_token)
        if st == 200:
            perms = body.get("permissions", {})
            push_ok = bool(perms.get("push"))
            print(
                f"    -> PASS read OK  permissions: push={perms.get('push')} "
                f"pull={perms.get('pull')} admin={perms.get('admin')}"
            )
            results["repo_read"] = True
            results["push_perm"] = push_ok
        else:
            print(f"    -> FAIL HTTP {st}: {body.get('error')}")
            results["repo_read"] = False

    # 4) Push proof — create then delete a throwaway ref (reversible)
    if slug and push_ok:
        print("[4] Push proof (create+delete throwaway ref) ...")
        st, body, _ = api("GET", f"/repos/{slug}/git/ref/heads/main", api_token)
        sha = body.get("object", {}).get("sha") if st == 200 else None
        if not sha:
            print(f"    -> SKIP could not read main SHA (HTTP {st})")
        else:
            ref = f"selftest-{int(time.time())}"
            st, body, _ = api(
                "POST", f"/repos/{slug}/git/refs", api_token,
                {"ref": f"refs/heads/{ref}", "sha": sha},
            )
            if st in (200, 201):
                dst, _, _ = api(
                    "DELETE", f"/repos/{slug}/git/refs/heads/{ref}", api_token
                )
                print(
                    f"    -> PASS created branch '{ref}' then deleted it "
                    f"(delete HTTP {dst}) - push/write confirmed"
                )
                results["push"] = True
            else:
                print(f"    -> FAIL create ref HTTP {st}: {body.get('error')}")
                results["push"] = False
    elif slug:
        print("[4] Push proof skipped - token lacks push permission on the repo.")

    _summary(results)


def _summary(results: dict) -> None:
    print("-" * 76)
    print("SUMMARY")
    labels = {
        "models": "GitHub Models (agent brain)",
        "identity": "GitHub API identity",
        "repo_read": "Repo read",
        "push_perm": "Push permission (repo says)",
        "push": "Push proof (branch create+delete)",
    }
    for k, label in labels.items():
        if k in results:
            print(f"  [{'PASS' if results[k] else 'FAIL'}] {label}")
    print("=" * 76)


if __name__ == "__main__":
    main()
