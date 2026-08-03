"""AI-brain provider selection — the swappable reasoning layer.

The predictive agent doesn't care *which* model reasons for it; it only needs a
brain that can turn grounded signals into a structured prediction. This factory
picks the provider from configuration so the same agent runs on:

* **GitHub Copilot CLI** — the approved brain for local demos / POCs (default).
* **Tardis / Chatflow** — the sanctioned brain for production (seam below).
* **GitHub Models** — legacy; retired upstream, kept only for compatibility.

Select with the ``BRAIN`` environment variable
(``copilot`` | ``tardis`` | ``github_models`` | ``auto``).
"""

from __future__ import annotations

import config
from agent.brain_base import BrainError
from agent.copilot_cli import CopilotCliBrain

__all__ = ["make_brain", "BrainError", "TardisBrain"]


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
    """Return the configured AI brain (defaults to the GitHub Copilot CLI)."""
    choice = (config.BRAIN or "auto").strip().lower()
    if choice in ("github_models", "github-models", "models"):
        from agent.github_models import GitHubModels

        return GitHubModels()
    if choice in ("tardis", "chatflow", "tardis_chatflow"):
        return TardisBrain()
    # "copilot", "copilot_cli", "auto", or anything else -> the demo brain
    return CopilotCliBrain()
