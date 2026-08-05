"""AI-brain provider selection — the swappable reasoning layer.

The predictive agent doesn't care *which* model reasons for it; it only needs a
brain that can turn grounded signals into a structured prediction. This factory
picks the provider from configuration so the same agent runs on:

* **GitHub Copilot CLI** — the approved brain for local demos / POCs (default).
* **Tardis / Chatflow** — the sanctioned brain for production (seam below).

Select with the ``BRAIN`` environment variable
(``copilot`` | ``tardis`` | ``auto``).
"""

from __future__ import annotations

import config
from agent.brain_base import BrainError
from agent.copilot_api import CopilotApiBrain
from agent.copilot_cli import CopilotCliBrain

__all__ = ["make_brain", "BrainError", "TardisBrain", "CopilotApiBrain"]


class TardisBrain:
    """Production brain seam (LNRS Tardis / Chatflow).

    Intentionally unimplemented here: wiring Tardis needs the internal Chatflow
    endpoint + credentials, which live in the production environment rather than
    this repo. It reports itself unavailable so the agent degrades gracefully
    until it is configured, and fails loudly with guidance if selected outright.
    """

    name = "tardis"

    def __init__(self) -> None:
        self.model = config.TARDIS_MODEL

    @property
    def available(self) -> bool:
        return False

    def chat(self, system: str, user: str, *, temperature: float = 0.1) -> str:
        raise BrainError(
            "Tardis/Chatflow brain is not configured in this environment. Wire the "
            "internal Chatflow endpoint + credentials into TardisBrain "
            "(agent/brain.py) for production use."
        )

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        raise BrainError("Tardis/Chatflow brain is not configured")


def make_brain():
    """Return the configured AI brain.

    Selection (``BRAIN`` env var):

    * ``copilot_api`` / ``opus`` / ``api`` -> Copilot REST API brain (Opus 4.8).
    * ``copilot_cli`` / ``cli``            -> Copilot CLI brain.
    * ``tardis`` / ``chatflow``            -> production Tardis seam.
    * ``auto`` / ``copilot`` (default)     -> prefer the Opus REST API brain when
      a Copilot credential is reachable, else the CLI brain (which itself falls
      back to the transparent heuristic when no CLI login is present).
    """
    choice = (config.BRAIN or "auto").strip().lower()
    if choice in ("tardis", "chatflow", "tardis_chatflow"):
        return TardisBrain()
    if choice in ("copilot_api", "copilot-api", "api", "opus", "rest"):
        return CopilotApiBrain()
    if choice in ("copilot_cli", "copilot-cli", "cli"):
        return CopilotCliBrain()
    # "auto" / "copilot" / anything else -> Opus REST API first, then the CLI.
    api = CopilotApiBrain()
    try:
        if api.available:
            return api
    except BrainError:
        pass
    return CopilotCliBrain()
