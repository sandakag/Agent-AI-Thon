"""Minimal Gemini GenerateContent client used as a final repair fallback."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import config
from agent.brain_base import BrainError


class GeminiBrain:
    """Gemini REST client with no third-party dependency."""

    name = "gemini"

    def __init__(self) -> None:
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.model = config.GEMINI_MODEL
        self.timeout = config.GEMINI_API_TIMEOUT

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, system: str, user: str, *, temperature: float = 0.1) -> str:
        if not self.available:
            raise BrainError("Gemini is not configured. Set GEMINI_API_KEY as a secret.")
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature},
        }
        request = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "Agent-AI-Thon/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BrainError(f"Gemini request failed ({exc.code}): {detail[:300]}") from exc
        except (URLError, OSError, json.JSONDecodeError) as exc:
            raise BrainError(f"Gemini request failed: {exc}") from exc
        try:
            parts = body["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts).strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise BrainError("Gemini returned no usable completion.") from exc
        if not text:
            raise BrainError("Gemini returned an empty completion.")
        return text
