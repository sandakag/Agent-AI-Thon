"""Shared foundation for the AI-brain providers (the pre/post-processing layer).

Kept import-free on purpose so every provider — and the factory in
:mod:`agent.brain` — can share one exception type without any circular imports.
"""

from __future__ import annotations


class BrainError(RuntimeError):
    """Raised when a brain provider is unreachable or returns nothing usable."""
