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
import tempfile
from pathlib import Path

from agent.brain_base import BrainError
from agent.copilot_api import CopilotApiBrain
from agent.copilot_cli import CopilotCliBrain
from agent.gemini import GeminiBrain
from agent.groq import GroqBrain
from agent.playbook import PlaybookBrain

ROOT = Path(__file__).resolve().parents[1]
# Groq's entry tier permits 12k tokens per minute. Keep the entire repair prompt
# comfortably below that limit so a failed CI can actually reach the fallback.
MAX_CONTEXT_CHARS = 24_000
MAX_FAILURE_LOG_CHARS = 12_000
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


# Lines that can legally appear inside a unified diff. Anything else (e.g. the
# model's trailing prose after the patch) ends the diff.
_DIFF_LINE = re.compile(r"^(diff --git |index |--- |\+\+\+ |@@ |\\|[ +-]|$)")


def extract_diff(text: str) -> str:
    """Pull ONLY the unified diff out of a model reply, dropping any commentary
    before or after it (a common cause of "patch with only garbage" failures)."""
    fenced = re.search(r"```(?:diff)?\s*\n(.*?)```", text, re.DOTALL)
    body = fenced.group(1) if fenced else text
    start = body.find("diff --git ")
    if start == -1:
        raise ValueError("The repair agent did not return a unified diff.")
    kept: list[str] = []
    for line in body[start:].splitlines():
        if kept and not _DIFF_LINE.match(line):
            break
        kept.append(line)
    diff = "\n".join(kept).rstrip("\n") + "\n"
    if diff.strip() == "diff --git":
        raise ValueError("The repair agent did not return a unified diff.")
    return diff


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


def _verify_patch(diff: str) -> None:
    """Raise if ``diff`` is unsafe or does not cleanly apply (dry run only)."""
    validate_diff(diff)
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as fh:
        fh.write(diff)
        tmp_path = fh.name
    try:
        subprocess.run(["git", "apply", "--check", tmp_path], cwd=ROOT, check=True,
                       capture_output=True, text=True)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def call_repair_agent(failure_log: str) -> str:
    """Ask each available brain in turn (local Copilot CLI, then the hosted
    Copilot API brain -- Claude Opus, authenticated via COPILOT_GITHUB_TOKEN --
    then Groq, then Gemini). The first reply that is a valid, appliable unified
    diff wins; a brain that answers with a malformed patch no longer fails the
    whole run, it just moves on to the next brain."""
    prompt = f"""You are a CI repair agent. Diagnose the failed Python unittest run below and return ONE minimal unified git diff that fixes the cause.

Hard rules:
- Change only existing .py source files or tests; do not change CI, dependencies, docs, secrets, or generated files.
- Change at most three files.
- Do not weaken, skip, delete, or mark tests expected-failure.
- Preserve public behavior except for the defect.
- Output only a diff beginning with 'diff --git'. No prose before or after it.

FAILED CI LOG:
{failure_log[:MAX_FAILURE_LOG_CHARS]}

REPOSITORY SOURCE:
{source_context()}
"""
    system = "You are a careful senior Python maintainer."
    # PlaybookBrain is a deterministic last resort: it only fires when the
    # failure log matches a signature it already has a verified fix for.
    brains = [CopilotCliBrain(), CopilotApiBrain(), GroqBrain(), GeminiBrain(), PlaybookBrain()]
    problems: list[str] = []
    for brain in brains:
        if not brain.available:
            continue
        try:
            reply = brain.chat(system, prompt, temperature=0.1)
            diff = extract_diff(reply)
            _verify_patch(diff)
        except (BrainError, ValueError, subprocess.CalledProcessError) as exc:
            problems.append(f"{brain.name}: {exc}")
            print(f"{brain.name} did not produce an appliable diff, trying next brain: {exc}",
                  file=sys.stderr)
            continue
        return diff
    detail = "; ".join(problems) if problems else (
        "no repair brain is configured -- sign in to the local Copilot CLI, or set "
        "COPILOT_GITHUB_TOKEN / GROQ_API_KEY / GEMINI_API_KEY as GitHub Actions secrets"
    )
    raise RuntimeError(f"No repair AI produced an appliable diff: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--failure-log", type=Path, required=True)
    args = parser.parse_args()
    failure_log = args.failure_log.read_text(encoding="utf-8", errors="replace")
    diff = call_repair_agent(failure_log)
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
