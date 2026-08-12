"""Minimal Groq chat-completions client used as an optional AI fallback."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import config
from agent.brain_base import BrainError


class GroqBrain:
    """OpenAI-compatible Groq provider with no third-party dependency."""

    name = "groq"

    def __init__(self) -> None:
        self.api_key = os.environ.get("GROQ_API_KEY", "")
        self.model = config.GROQ_MODEL
        self.timeout = config.GROQ_API_TIMEOUT

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, system: str, user: str, *, temperature: float = 0.1) -> str:
        if not self.available:
            raise BrainError("Groq is not configured. Set GROQ_API_KEY as a secret.")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        request = Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BrainError(f"Groq request failed ({exc.code}): {detail[:300]}") from exc
        except (URLError, OSError, json.JSONDecodeError) as exc:
            raise BrainError(f"Groq request failed: {exc}") from exc
        try:
            text = body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise BrainError("Groq returned no usable completion.") from exc
        if not text:
            raise BrainError("Groq returned an empty completion.")
        return text

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        text = self.chat(system, user, temperature=temperature)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            return {}
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
