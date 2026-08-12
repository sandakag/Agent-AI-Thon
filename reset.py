"""Prepare a repeatable two-phase demo starting state.

Run ``python reset.py`` after pulling ``main`` and before committing the demo
reset.  It changes only local, generated demo state and ``pipeline/etl.py``;
it deliberately does not commit, push, create GitHub artifacts, or merge
anything.  The presenter retains control of the subsequent push and review.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import config
from incidents import KNOWN_RUNTIME_INCIDENTS
from reset_stacks import reset_local


_BASELINE = config.ROOT / "pipeline" / "etl_baseline.py"
_TARGET = config.ROOT / "pipeline" / "etl.py"
_SIZE_LINE = '        size = _to_float(r.get("size"))'
_SIZE_COMPATIBLE = '        size = _to_float(r.get("size") if r.get("size") is not None else r.get("qty"))'


def phase_one_demo_source(baseline: str) -> str:
    """Return the safe reset source for the CI demonstration.

    The only deliberate CI fault is the missing ``price -> px`` alias.  The
    companion ``qty`` alias remains present so the known-fix playbook can repair
    one minimal line and turn CI green after a human merges its PR.  The remaining
    baseline resilience gaps are intentionally left for Phase 2's runtime demo.
    """
    if _SIZE_LINE not in baseline:
        raise ValueError("Unexpected ETL baseline; refusing to create demo state.")
    source = baseline.replace(_SIZE_LINE, _SIZE_COMPATIBLE, 1)
    if 'price = _to_float(r.get("price"))' not in source:
        raise ValueError("Expected Phase 1 price-alias fault is missing from baseline.")
    return source


def prepare() -> None:
    """Reset local artifacts and write the deterministic Phase 1/2 starting state."""
    baseline = _BASELINE.read_text(encoding="utf-8")
    _TARGET.write_text(phase_one_demo_source(baseline), encoding="utf-8")
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
    print("Phase 1: pipeline/etl.py now contains the controlled px-alias CI fault.")
    print("Phase 2: six runtime incident presets are restored in data/demo_incidents.json.")
    for item in KNOWN_RUNTIME_INCIDENTS:
        print(f"    - {item['button']:18s} {item['id']}")
    if not args.full:
        print("(run with --full to also close old guardian issues/PRs + purge Grafana)")
    print("Review and push this reset commit yourself; the system never pushes or merges it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
