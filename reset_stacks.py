"""Reset the demo to a pristine GREEN start — run this between audiences.

Clears all three stacks in one shot:
  * GitHub  — closes every guardian-created issue + PR and deletes the
              ``guardian/fix-*`` branches (surgical: nothing else is touched).
  * Grafana — deletes every guardian-tagged annotation and resolves any open
              guardian IRM incident.
  * Local   — resets the dashboard/loop state files (banner, RCA history, chat,
              signals, warehouse, agent memory) and truncates the audit trail.

Best-effort: a stack with no credentials configured is simply skipped, so this
is always safe to run. Intended to be executed INSIDE the guardian container
(``docker exec agent-aithon-guardian-loop-1 python -m reset_stacks``) so the
GitHub token + in-network Grafana URL resolve correctly.
"""

from __future__ import annotations

import json

import config
from governance import github_gov, grafana_gov

# (path, empty-value) for every local state file the dashboard/loop reads.
_JSON_RESET = [
    (config.INCIDENTS_FILE, {"level": "GREEN"}),
    (config.DATA_DIR / "rca_history.json", []),
    (config.DATA_DIR / "chat_log.json", []),
    (config.SIGNAL_HISTORY_FILE, []),
    (config.WAREHOUSE_FILE, {}),
    (config.MEMORY_FILE, []),
    (config.DATA_DIR / "grafana_incidents.json", {}),
]
# Transient files the engine recreates from scratch — just remove them.
_DELETE = [
    config.GUARDIAN_STATE_FILE,
    config.DATA_DIR / "pending_incident.json",
]


def reset_local() -> None:
    for path, empty in _JSON_RESET:
        try:
            path.write_text(json.dumps(empty), encoding="utf-8")
        except OSError as exc:
            print(f"    -> [cleanup] could not reset {path.name}: {exc}")
    for path in _DELETE:
        try:
            path.unlink()
        except OSError:
            pass
    for log in (config.AUDIT_LOG, config.STREAM_LOG):
        try:
            log.write_text("", encoding="utf-8")
        except OSError:
            pass
    print("    -> [cleanup] Local: banner GREEN, RCA/chat/signals/warehouse/memory "
          "cleared, audit trail truncated")


def restore_baseline_source() -> None:
    """Put ``pipeline/etl.py`` back to the pristine, UN-hardened demo baseline —
    locally AND on GitHub — so the next injected incident can stage a REAL code-fix
    PR again. Without this, once a guardian PR is merged the fix is already in the
    file and every later run correctly finds "nothing to change" (no PR)."""
    baseline = config.ROOT / "pipeline" / "etl_baseline.py"
    target = config.ROOT / "pipeline" / "etl.py"
    try:
        content = baseline.read_text(encoding="utf-8")
    except OSError:
        print("    -> [cleanup] no pipeline/etl_baseline.py — source left as-is")
        return
    try:
        if target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
        print("    -> [cleanup] Source: pipeline/etl.py restored to the demo baseline")
    except OSError as exc:
        print(f"    -> [cleanup] could not restore pipeline/etl.py: {exc}")
    if github_gov.fetch_main_source() != content:
        ok = github_gov.push_source(
            content, "demo reset: restore pipeline/etl.py to the pristine baseline")
        print(f"    -> [cleanup] GitHub: baseline pushed to main ({'ok' if ok else 'FAILED'})")
    else:
        print("    -> [cleanup] GitHub: main already at the baseline")


def main() -> None:
    print("Resetting the Predictive Pipeline Guardian demo to pristine GREEN...")
    github_gov.cleanup_all()
    restore_baseline_source()
    grafana_gov.purge_annotations()
    grafana_gov.resolve_incidents()
    reset_local()
    print("Reset complete — inject a fresh incident whenever you're ready.")


if __name__ == "__main__":
    main()
