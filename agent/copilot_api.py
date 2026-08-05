"""GitHub Copilot **REST API** brain — Claude Opus 4.8 via your subscription.

This is the high-power reasoning brain. Unlike the Copilot *CLI* brain
(``agent/copilot_cli.py``), which shells out to the ``copilot`` binary and — on
some seats — cannot force a premium model with ``--model``, this brain talks
directly to the Copilot Chat REST API (``/chat/completions``) exactly like the
VS Code Copilot Chat extension does. That gives full control of the model
(``claude-opus-4.8`` by default), the temperature and the output budget, so the
guardian can produce a *complete, detailed* RCA and answer operator questions
with the full strength of Opus.

How it authenticates (no PAT, no API key — your existing Copilot subscription):

  1. Find a GitHub OAuth token (first that works wins):
       * ``COPILOT_OAUTH_TOKEN`` / ``GITHUB_COPILOT_TOKEN`` env vars,
       * a token file at ``COPILOT_TOKEN_STORE`` (``{"access_token": "..."}``),
       * the standalone device-login cache ``~/.copilot-standalone/token.json``,
       * ``apps.json`` / ``hosts.json`` written by the official Copilot plugins
         (``%LOCALAPPDATA%/github-copilot`` etc.).
  2. Exchange it for a short-lived Copilot **session token** at
     ``api.github.com/copilot_internal/v2/token`` — the response also tells us
     which API host to use (Enterprise seats get their own host).
  3. Call ``{api_base}/chat/completions`` with the VS Code editor-identity
     headers (newer models are gated behind a recent editor version).

Everything is pure standard library (``urllib``) so it runs on the host and in
the headless Docker container with no extra dependencies. If no credential is
available the brain reports ``available == False`` and the agent degrades to the
transparent grounded heuristic, so nothing ever breaks.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import config
from agent.brain_base import BrainError

# OAuth client id of the official GitHub Copilot app + the token-exchange host.
_SESSION_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
_DEFAULT_API_BASE = "https://api.githubcopilot.com"

# Editor identity — the API gates newer models behind a recent editor build, so
# we present ourselves as a current VS Code Copilot Chat client (same as the
# reference standalone client).
_EDITOR_VERSION = os.environ.get("COPILOT_EDITOR_VERSION", "vscode/1.104.0")
_PLUGIN_VERSION = os.environ.get("COPILOT_PLUGIN_VERSION", "copilot-chat/0.26.0")
_USER_AGENT = os.environ.get("COPILOT_USER_AGENT", "GithubCopilot/1.104.0")
_INTEGRATION_ID = os.environ.get("COPILOT_INTEGRATION_ID", "vscode-chat")


def _plugin_config_dirs() -> list[Path]:
    home = Path.home()
    out: list[Path] = []
    if os.name == "nt":
        for var in ("LOCALAPPDATA", "APPDATA"):
            base = os.getenv(var)
            if base:
                out.append(Path(base) / "github-copilot")
    out.append(home / ".config" / "github-copilot")
    return [d for d in out if d.is_dir()]


def _plugin_tokens() -> list[str]:
    tokens: list[str] = []
    for directory in _plugin_config_dirs():
        for name in ("apps.json", "hosts.json"):
            path = directory / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, dict) and str(key).startswith("github.com"):
                        tok = value.get("oauth_token")
                        if tok:
                            tokens.append(tok)
    return tokens


def _read_token_file(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        return data.get("access_token") or data.get("oauth_token")
    return None


def _candidate_oauth_tokens() -> list[str]:
    """Ordered, de-duplicated OAuth-token candidates from every known source."""
    seen: set[str] = set()
    out: list[str] = []

    def add(tok: str | None) -> None:
        tok = (tok or "").strip()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)

    add(os.environ.get("COPILOT_OAUTH_TOKEN"))
    add(os.environ.get("GITHUB_COPILOT_TOKEN"))
    store = os.environ.get("COPILOT_TOKEN_STORE") or config.COPILOT_TOKEN_STORE
    if store:
        add(_read_token_file(Path(store)))
    add(_read_token_file(Path.home() / ".copilot-standalone" / "token.json"))
    for tok in _plugin_tokens():
        add(tok)
    return out


def _http(method: str, url: str, headers: dict, body: dict | None = None,
          timeout: float = 30.0) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except OSError:
            return exc.code, ""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, str(exc)


class CopilotApiBrain:
    """Reasoning brain backed by the Copilot Chat REST API (Claude Opus 4.8)."""

    name = "copilot-api"

    def __init__(self) -> None:
        self.model = config.COPILOT_MODEL
        self._oauth: str | None = None
        self._session_token: str | None = None
        self._api_base: str = _DEFAULT_API_BASE
        self._session_exp: float = 0.0
        self._enabled_models: set[str] = set()
        self._avail: bool | None = None

    # -- availability ----------------------------------------------------
    @property
    def available(self) -> bool:
        """True once a Copilot session has been obtained (cached, one probe)."""
        if self._avail is None:
            try:
                self._avail = self._session() is not None
            except BrainError:
                self._avail = False
        return bool(self._avail)

    # -- session management ---------------------------------------------
    def _session(self) -> str | None:
        if self._session_token and time.time() < self._session_exp - 60:
            return self._session_token
        errors: list[str] = []
        for oauth in _candidate_oauth_tokens():
            status, text = _http(
                "GET", _SESSION_TOKEN_URL,
                {
                    "Authorization": f"token {oauth}",
                    "Accept": "application/json",
                    "Editor-Version": _EDITOR_VERSION,
                    "Editor-Plugin-Version": _PLUGIN_VERSION,
                    "User-Agent": _USER_AGENT,
                },
                timeout=config.COPILOT_API_TIMEOUT,
            )
            if status in (401, 403, 404):
                errors.append(f"{status}")
                continue
            if status != 200:
                errors.append(f"{status}:{text[:80]}")
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                errors.append("bad-json")
                continue
            token = data.get("token")
            if not token:
                errors.append("no-token")
                continue
            self._oauth = oauth
            self._session_token = token
            self._api_base = (data.get("endpoints", {}) or {}).get(
                "api", _DEFAULT_API_BASE).rstrip("/")
            self._session_exp = float(data.get("expires_at", time.time() + 1500))
            return token
        if errors:
            raise BrainError("Copilot session exchange failed (tried "
                             f"{len(errors)} credential(s): {', '.join(errors[:4])})")
        return None  # no credentials at all

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._session_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Editor-Version": _EDITOR_VERSION,
            "Editor-Plugin-Version": _PLUGIN_VERSION,
            "User-Agent": _USER_AGENT,
            "Copilot-Integration-Id": _INTEGRATION_ID,
        }

    # -- chat ------------------------------------------------------------
    def chat(self, system: str, user: str, *, temperature: float = 0.1) -> str:
        token = self._session()
        if not token:
            raise BrainError("No GitHub Copilot credential found. Sign in on the "
                             "host (python -m copilot login) or set "
                             "GITHUB_COPILOT_TOKEN / COPILOT_TOKEN_STORE.")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        text = self._complete(messages, temperature, with_max_tokens=True)
        return text.strip()

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        reply = self.chat(system, user, temperature=temperature)
        return _extract_json(reply)

    def _complete(self, messages: list, temperature: float,
                  with_max_tokens: bool) -> str:
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature}
        if with_max_tokens and config.COPILOT_MAX_TOKENS > 0:
            payload["max_tokens"] = config.COPILOT_MAX_TOKENS

        status, text = _http("POST", f"{self._api_base}/chat/completions",
                             self._headers(), payload,
                             timeout=config.COPILOT_API_TIMEOUT)

        # A premium model may need its usage policy accepted once per account.
        if status == 400 and "model_not_supported" in text and \
                self.model not in self._enabled_models:
            self._enable_model(self.model)
            status, text = _http("POST", f"{self._api_base}/chat/completions",
                                 self._headers(), payload,
                                 timeout=config.COPILOT_API_TIMEOUT)

        # Some hosts reject an explicit max_tokens — retry once without it.
        if status == 400 and with_max_tokens and "max_tokens" in text:
            return self._complete(messages, temperature, with_max_tokens=False)

        if status != 200:
            raise BrainError(f"Copilot chat failed: {status} {text[:200]}")
        try:
            data = json.loads(text)
            return data["choices"][0]["message"]["content"] or ""
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise BrainError(f"Copilot chat returned no usable content: {exc}")

    def _enable_model(self, model: str) -> None:
        _http("POST", f"{self._api_base}/models/{model}/policy",
              self._headers(), {"state": "enabled"},
              timeout=config.COPILOT_API_TIMEOUT)
        self._enabled_models.add(model)
        # policy is embedded in the session token -> force a refresh
        self._session_token = None
        self._session_exp = 0.0
        self._session()


def _extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of the model's reply."""
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
