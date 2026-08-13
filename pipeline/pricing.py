"""Revenue rounding helper — presentation-layer rounding for reported revenue.

Deliberately independent of ``pipeline/etl.py`` (parse/aggregate/load): this is
the Phase 1 CI-demo fault surface. Because Phase 1's failing test and its fix
live ENTIRELY here, healing it can never touch or re-harden the Phase 2
runtime-incident vulnerability surface in ``pipeline/etl.py``. The two demo
phases can therefore run back-to-back from a single ``reset.py`` with no reset
in between.
"""
from __future__ import annotations


def round_amount(amount: float) -> float:
    """Round a computed trade amount to the nearest cent (2 decimal places)."""
    return round(amount, 2)
