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


def main() -> None:
    print("Resetting the Predictive Pipeline Guardian demo to pristine GREEN...")
    github_gov.cleanup_all()
    grafana_gov.purge_annotations()
    grafana_gov.resolve_incidents()
    reset_local()
    print("Reset complete — inject a fresh incident whenever you're ready.")


if __name__ == "__main__":
    main()
