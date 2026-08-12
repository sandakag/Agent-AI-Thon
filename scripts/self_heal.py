#!/usr/bin/env python3
"""Generate and apply a tightly scoped AI repair patch for a failed CI run.

This program intentionally has no third-party dependencies. It sends the failed
test output and repository Python sources to the authenticated local GitHub
Copilot CLI, accepts only one unified diff, rejects changes outside safe
source/test files, and lets the workflow run the full test command before a PR
can be opened.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from agent.copilot_cli import CopilotCliBrain, CopilotCliError

ROOT = Path(__file__).resolve().parents[1]
MAX_CONTEXT_CHARS = 120_000
ALLOWED_PREFIXES = ("agent/", "alerting/", "governance/", "ingestion/", "pipeline/", "policy/", "signals/", "tests/")
ALLOWED_TOP_LEVEL = {"config.py", "dashboard.py", "faults.py", "guardian_loop.py", "run_demo.py", "verify_access.py"}


def source_context() -> str:
    """Return bounded, text-only context; never include secrets or CI files."""
    chunks: list[str] = []
    used = 0
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith((".git/", "scripts/")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        part = f"\n### {relative}\n{text}\n"
        if used + len(part) > MAX_CONTEXT_CHARS:
            break
        chunks.append(part)
        used += len(part)
    return "".join(chunks)


def extract_diff(text: str) -> str:
    match = re.search(r"(?:```diff\s*)?(diff --git .+?)(?:```\s*)?$", text, re.DOTALL)
    if not match:
        raise ValueError("The repair agent did not return a unified diff.")
    return match.group(1).strip() + "\n"


def changed_paths(diff: str) -> set[str]:
    paths = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            paths.add(line[6:])
    return paths


def validate_diff(diff: str) -> None:
    """Prevent the agent from changing workflow, dependency, or secret surfaces."""
    if "GITHUB_" in diff or "COPILOT_" in diff:
        raise ValueError("Repair diff attempted to modify credential-related content.")
    paths = changed_paths(diff)
    if not paths or len(paths) > 3:
        raise ValueError("A repair must modify between one and three source/test files.")
    for path in paths:
        allowed = path in ALLOWED_TOP_LEVEL or path.startswith(ALLOWED_PREFIXES)
        if not allowed or not path.endswith(".py") or path.startswith((".github/", "scripts/")):
            raise ValueError(f"Unsafe repair target rejected: {path}")


def call_copilot(failure_log: str) -> str:
    """Ask the presenter's signed-in local Copilot CLI to author the repair."""
    brain = CopilotCliBrain()
    if not brain.available:
        raise RuntimeError(
            "Local GitHub Copilot CLI is unavailable. Sign in on this self-hosted "
            "runner before enabling self-healing."
        )
    prompt = f"""You are a CI repair agent. Diagnose the failed Python unittest run below and return ONE minimal unified git diff that fixes the cause.

Hard rules:
- Change only existing .py source files or tests; do not change CI, dependencies, docs, secrets, or generated files.
- Change at most three files.
- Do not weaken, skip, delete, or mark tests expected-failure.
- Preserve public behavior except for the defect.
- Output only a diff beginning with 'diff --git'.

FAILED CI LOG:
{failure_log[:30_000]}

REPOSITORY SOURCE:
{source_context()}
"""
    try:
        return brain.chat(
            "You are GitHub Copilot acting as a careful senior Python maintainer.",
            prompt,
            temperature=0.1,
        )
    except CopilotCliError as exc:
        raise RuntimeError(f"Local GitHub Copilot CLI failed: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--failure-log", type=Path, required=True)
    args = parser.parse_args()
    failure_log = args.failure_log.read_text(encoding="utf-8", errors="replace")
    diff = extract_diff(call_copilot(failure_log))
    validate_diff(diff)
    patch = ROOT / ".self-heal.patch"
    patch.write_text(diff, encoding="utf-8")
    try:
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=ROOT, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=ROOT, check=True)
    finally:
        patch.unlink(missing_ok=True)
    print("Applied validated self-heal patch.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Self-heal stopped safely: {exc}", file=sys.stderr)
        raise SystemExit(1)
