"""Prepare a repeatable two-phase demo starting state — reset ONCE at the start.

Run ``python reset.py`` after pulling ``main`` and before committing the demo
reset. It changes only local, generated demo state, ``pipeline/pricing.py``
(Phase 1's CI fault) and ``pipeline/etl.py`` (Phase 2's runtime-incident
vulnerability); it deliberately does not commit, push, create GitHub artifacts,
or merge anything. The presenter retains control of the subsequent push and
review.

Phase 1's CI fault lives ENTIRELY in ``pipeline/pricing.py``, a module
independent of ``pipeline/etl.py``. Healing it (the self-heal ML known-fix
analyzer) only ever touches ``pricing.py``, so it can never re-harden the
``pipeline/etl.py`` vulnerability Phase 2 needs. Both phases are therefore
ready immediately after this ONE reset — no reset is needed between them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import config
from incidents import KNOWN_RUNTIME_INCIDENTS
from reset_stacks import reset_local


_ETL_BASELINE = config.ROOT / "pipeline" / "etl_baseline.py"
_ETL_TARGET = config.ROOT / "pipeline" / "etl.py"
_PRICING_TARGET = config.ROOT / "pipeline" / "pricing.py"
_PRICING_FIXED = "    return round(amount, 2)\n"
_PRICING_BROKEN = "    return round(amount)\n"


def phase_one_demo_source(pricing_source: str) -> str:
    """Return ``pipeline/pricing.py`` with the ONE controlled, independent CI
    fault: rounding silently drops its decimal-places argument. This never
    touches ``pipeline/etl.py``, so Phase 2's runtime-incident vulnerability is
    unaffected by injecting or by healing this fault.

    Idempotent: re-running the reset when the fault is already present is a
    no-op, so ``reset.py`` is always safe to run repeatedly."""
    if _PRICING_BROKEN in pricing_source:
        return pricing_source
    if _PRICING_FIXED not in pricing_source:
        raise ValueError("Unexpected pipeline/pricing.py; refusing to create demo state.")
    return pricing_source.replace(_PRICING_FIXED, _PRICING_BROKEN, 1)


def prepare() -> None:
    """Reset local artifacts and write the deterministic Phase 1 + Phase 2
    starting state in one shot (no reset needed between the two phases)."""
    # Phase 1: the ONE independent CI fault, isolated to pricing.py.
    pricing_source = _PRICING_TARGET.read_text(encoding="utf-8")
    _PRICING_TARGET.write_text(phase_one_demo_source(pricing_source), encoding="utf-8")

    # Phase 2: pipeline/etl.py starts VULNERABLE so a runtime incident can be
    # induced immediately after Phase 1 completes -- no reset in between.
    baseline = _ETL_BASELINE.read_text(encoding="utf-8")
    _ETL_TARGET.write_text(baseline, encoding="utf-8")

    reset_local()
    catalog_path = config.DATA_DIR / "demo_incidents.json"
    catalog_path.write_text(json.dumps(KNOWN_RUNTIME_INCIDENTS, indent=2), encoding="utf-8")


def cleanup_remote() -> None:
    """OPTIONAL (``--full``): close every guardian-created GitHub issue/PR, delete
    the ``guardian/fix-*`` branches, and purge Grafana annotations/incidents so a
    repeat demo starts from a clean slate. Best-effort; skipped when credentials
    are absent. This closes AI-created artifacts only — it never merges anything."""
    from governance import github_gov, grafana_gov
    try:
        github_gov.cleanup_all()
    except Exception as exc:  # noqa: BLE001 - best-effort, never abort the reset
        print(f"    -> [reset] GitHub cleanup skipped: {exc}")
    try:
        grafana_gov.purge_annotations()
        grafana_gov.resolve_incidents()
    except Exception as exc:  # noqa: BLE001
        print(f"    -> [reset] Grafana cleanup skipped: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset the demo to its repeatable vulnerable starting state.")
    parser.add_argument(
        "--full", action="store_true",
        help="also close guardian GitHub issues/PRs + purge Grafana (clean repeat)")
    args = parser.parse_args()

    try:
        prepare()
    except (OSError, ValueError) as exc:
        print(f"Demo reset stopped safely: {exc}")
        return 1
    if args.full:
        cleanup_remote()

    print("Demo reset complete.")
    print("Phase 1: pipeline/pricing.py now contains the controlled, independent CI fault.")
    print("Phase 2: pipeline/etl.py is now VULNERABLE and six runtime incidents are restored.")
    print("Both phases are ready now -- no reset needed between Phase 1 and Phase 2.")
    for item in KNOWN_RUNTIME_INCIDENTS:
        print(f"    - {item['button']:18s} {item['id']}")
    if not args.full:
        print("(run with --full to also close old guardian issues/PRs + purge Grafana)")
    print("Review and push this reset commit yourself; the system never pushes or merges it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
