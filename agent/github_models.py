"""Minimal, dependency-free GitHub Models client (OpenAI-compatible).

Mirrors the itsm-self-heal-ai client so the predictive agent binds to the same
approved org tool. Talks to the endpoint with the standard-library ``urllib``
only — no ``openai`` package, nothing to install. The token is read lazily and
never logged; responses are parsed defensively so a bad reply can't crash us.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import config

from agent.brain_base import BrainError


class GitHubModelsError(BrainError):
    """Raised when the model cannot be reached or returns nothing usable."""


class GitHubModels:
    name = "github-models"

    def __init__(self, token: str | None = None, model: str | None = None) -> None:
        self._token = token or os.environ.get("GITHUB_MODELS_TOKEN")
        self.model = model or config.GITHUB_MODEL
        self.endpoint = config.GITHUB_MODELS_ENDPOINT
        self.timeout = config.LLM_TIMEOUT_SECONDS

    @property
    def available(self) -> bool:
        return bool(self._token)

    def chat(self, system: str, user: str, *, temperature: float = 0.1) -> str:
        if not self._token:
            raise GitHubModelsError(
                "No Models token set — set GITHUB_MODELS_TOKEN (the enterprise Models token)"
            )
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GitHubModelsError(f"GitHub Models HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise GitHubModelsError(f"GitHub Models unreachable: {exc}") from exc
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GitHubModelsError("Malformed response from GitHub Models") from exc

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        return _extract_json(self.chat(system, user, temperature=temperature))


def _extract_json(text: str) -> dict:
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
