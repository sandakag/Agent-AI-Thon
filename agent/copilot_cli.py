"""GitHub Copilot CLI brain — the approved-tool reasoning engine for demos.

This is the "pre/post-processing extension layer" around GitHub Copilot: we build
the prompt (pre), invoke the GitHub Copilot CLI for the reasoning step, then parse
its reply defensively (post). It runs under the user's approved GitHub Copilot
seat via the CLI that ships with the VS Code Copilot extension — no extra service
to provision and no dependency on the retired GitHub Models endpoint.

Exposes the standard brain interface (``available`` / ``chat`` / ``chat_json``
/ ``model`` / ``name``) so it is a drop-in reasoning brain for the predictive
agent.

Notes
-----
* The Copilot CLI is an *agentic* tool: expect a few seconds up to ~a minute of
  latency per call. That is fine for a periodic self-healer / demo, but not for
  high-throughput production — for production, select the Tardis provider
  (see :mod:`agent.brain`).
* Each call runs from a throwaway temp directory so any tool use the agent might
  perform cannot touch this repository.
* The prompt is handed to PowerShell via an environment variable, never on the
  command line, so incident text can never be mis-quoted or injected.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import config
from agent.brain_base import BrainError

log = logging.getLogger(__name__)


class CopilotCliError(BrainError):
    """Raised when the Copilot CLI is missing, unauthenticated, or unusable."""


# Non-interactive invocation flags. ``-s`` (silent) makes the CLI print only the
# model's answer; ``--allow-all-tools`` is required so it never blocks on an
# approval prompt when run head-less (we contain it in a throwaway temp cwd).
_FLAGS = [
    "-s",
    "--allow-all-tools",
    "--no-color",
    "--disable-builtin-mcps",
    "--no-custom-instructions",
    "--no-ask-user",
    "--log-level",
    "none",
]

# Model ids we pass through to `copilot --model` must look like a model slug
# (letters/digits/.-_+). Anything else is refused so a stray config/env value can
# never be smuggled into the PowerShell `-Command` string.
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
# `--effort` levels the CLI accepts (higher = more detailed reasoning).
_EFFORT_LEVELS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
# Values meaning "let Copilot pick": we omit `--model` so behaviour matches the
# plain CLI default (works on every Copilot plan). "cli" is the legacy label.
_AUTO_MODELS = {"", "auto", "cli"}


def _model_unavailable(*texts: str) -> bool:
    """True when the CLI rejected `--model` because the seat lacks that model."""
    blob = " ".join(t for t in texts if t).lower()
    return "model" in blob and (
        "not available" in blob or "not supported" in blob or "no access" in blob
    )


class CopilotCliBrain:
    name = "github-copilot"

    def __init__(self, model: str | None = None) -> None:
        # Preferred model requested via `copilot --model`. Premium models such as
        # `claude-opus-4.8` need a Copilot Pro+/Business/Enterprise/Max seat; on
        # Free/Pro the CLI rejects them, so we fall back automatically.
        self.preferred_model = (model or config.COPILOT_CLI_MODEL or "auto").strip()
        self.fallback_model = (config.COPILOT_CLI_FALLBACK_MODEL or "auto").strip()
        self.reasoning_effort = (config.COPILOT_CLI_REASONING_EFFORT or "").strip().lower()
        # Effective model actually used -- updated after a fallback so the audit
        # trail (`self.llm.model`) always reflects what really ran.
        self.model = self.preferred_model
        self.timeout = config.COPILOT_CLI_TIMEOUT_SECONDS
        self._exe = _find_copilot()
        # Cached working model for this process: once a model is accepted (or we
        # fall back), later calls skip the ~12s "model not available" probe.
        self._resolved_model: str | None = None

    @property
    def available(self) -> bool:
        return bool(self._exe)

    def _model_flags(self, model: str) -> list[str]:
        """Validated `--model` / `--effort` flags for a single invocation."""
        flags: list[str] = []
        candidate = (model or "").strip()
        if candidate.lower() not in _AUTO_MODELS and _SAFE_MODEL.match(candidate):
            flags += ["--model", candidate]
        if self.reasoning_effort in _EFFORT_LEVELS:
            flags += ["--effort", self.reasoning_effort]
        return flags

    def _run(self, prompt: str, model: str) -> tuple[int, str, str]:
        """Invoke the Copilot CLI once for `model`. Returns (returncode, out, err)."""
        env = os.environ.copy()
        env["COP_PROMPT"] = prompt
        # The Copilot CLI authenticates with its own stored OAuth login. If a
        # GitHub PAT is present in the environment (e.g. a leftover GITHUB_TOKEN
        # from the retired GitHub Models client), the CLI prefers it and fails
        # with "Authentication failed" when that token is stale/under-scoped.
        # Drop those vars so the CLI always uses its valid interactive login.
        for tok in ("GITHUB_TOKEN", "GH_TOKEN", "GH_COPILOT_TOKEN", "GITHUB_COPILOT_TOKEN"):
            env.pop(tok, None)
        model_flags = self._model_flags(model)
        if os.name == "nt":
            # Windows: the authenticated CLI is the VS Code shim, which only
            # yields captured output when invoked *by name* through PowerShell 7
            # (pwsh) — calling the .ps1 by full path returns nothing. We put its
            # directory first on PATH so `copilot` resolves to it, and hand the
            # prompt over in $env:COP_PROMPT (never on the command line). The
            # model flags are validated slugs, so they are safe to inline here.
            shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            shim_dir = os.path.dirname(self._exe)
            if shim_dir:
                env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
            inner = "copilot -p $env:COP_PROMPT " + " ".join([*model_flags, *_FLAGS])
            cmd = [shell, "-NoProfile", "-Command", inner]
        else:
            cmd = [self._exe, "-p", prompt, *model_flags, *_FLAGS]
        with tempfile.TemporaryDirectory(prefix="copilot_brain_") as workdir:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=workdir,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise CopilotCliError(
                    f"Copilot CLI timed out after {self.timeout}s"
                ) from exc
            except (OSError, ValueError) as exc:
                raise CopilotCliError(f"Copilot CLI failed to launch: {exc}") from exc
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")

    def chat(self, system: str, user: str, *, temperature: float = 0.1) -> str:
        if not self._exe:
            raise CopilotCliError(
                "GitHub Copilot CLI not found. It ships with the VS Code Copilot "
                "extension; otherwise set COPILOT_CLI_PATH to the executable."
            )
        prompt = f"{system}\n\n{user}"
        model = self._resolved_model or self.preferred_model
        returncode, out, err = self._run(prompt, model)
        # If the preferred model is not on this Copilot seat, fall back once to
        # COPILOT_CLI_FALLBACK_MODEL (default "auto"), then remember the choice so
        # we never pay the rejection latency again this process.
        if (
            returncode != 0
            and _model_unavailable(out, err)
            and self.fallback_model.lower() != model.lower()
        ):
            log.warning(
                "Copilot model %r is not available on this seat; falling back to %r. "
                "Premium models (e.g. claude-opus-4.8) require a Copilot "
                "Pro+/Business/Enterprise/Max subscription.",
                model,
                self.fallback_model,
            )
            model = self.fallback_model
            self._resolved_model = model
            self.model = model
            returncode, out, err = self._run(prompt, model)
        if returncode != 0:
            detail = (err or out or "").strip().replace("\n", " ")
            raise CopilotCliError(f"Copilot CLI exited {returncode}: {detail[:200]}")
        text = out.strip()
        if not text:
            detail = err.strip().replace("\n", " ")
            raise CopilotCliError(
                "Copilot CLI returned no output" + (f": {detail[:300]}" if detail else "")
            )
        if self._resolved_model is None:
            self._resolved_model = model
            self.model = model
        return text

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        return _extract_json(self.chat(system, user, temperature=temperature))


def _find_copilot() -> str | None:
    """Locate an authenticated Copilot CLI: explicit override, VS Code shim, PATH."""
    override = os.environ.get("COPILOT_CLI_PATH") or config.COPILOT_CLI_PATH
    if override and os.path.exists(override):
        return override
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata)
        for code_dir in ("Code", "Code - Insiders"):
            shim_dir = (
                base
                / code_dir
                / "User"
                / "globalStorage"
                / "github.copilot-chat"
                / "copilotCli"
            )
            # Prefer a launcher that sets up auth (.bat/.cmd/.exe), then the .ps1.
            for fname in ("copilot.bat", "copilot.cmd", "copilot.exe", "copilot.ps1", "copilot"):
                shim = shim_dir / fname
                if shim.exists():
                    return str(shim)
    return shutil.which("copilot")


def _extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of the model's reply."""
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
