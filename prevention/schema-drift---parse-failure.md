# Preventive remediation — schema-drift / parse failure

_Staged by the Predictive Pipeline Guardian at 2026-07-31T10:32:08.092006+00:00. Gated — a human approves the merge._

- **Severity:** RED
- **Risk score:** 86/100
- **Predicted lead time:** ~0 min
- **Confidence:** 0.78
- **Reasoned by:** `github-copilot:cli`

## Evidence
- schema_drift=true (schema_hash mismatch)
- null_rate=0.1667 with positive slope=0.0833 (r2=0.75)
- tool_risk_estimate=86 predicting schema-drift
- high processing latency 4372.7ms
- similar past incidents linked schema_drift to parse failures

## Recommended action
Pause downstream consumers, enforce schema validation at ingress, roll back or block recent schema change and deploy parser fix, run contract tests and backfill nulls, closely monitor null_rate and parse errors
