# Preventive remediation — schema-drift/parse-failure

_Staged by the Predictive Pipeline Guardian at 2026-07-31T10:35:25.999444+00:00. Gated — a human approves the merge._

- **Severity:** RED
- **Risk score:** 99/100
- **Predicted lead time:** ~0 min
- **Confidence:** 0.95
- **Reasoned by:** `github-copilot:cli`

## Evidence
- null_rate=0.8233
- null_slope_per_tick=0.1118 (rising)
- null_trend_r2=0.98 (strong)
- schema_drift=true (schema_hash=fb9d660bff68)
- etl_failed=true
- latency_ms=3690.3
- tool_risk_estimate=99.0
- similar_past_incidents: schema-drift with records=300

## Recommended action
Pause ingestion; alert on-call; run schema-validator and revert to last-known-good mapping; reprocess from validated snapshot after fix.
