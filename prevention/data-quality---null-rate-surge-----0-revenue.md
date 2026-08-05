# Preventive remediation — data-quality / null-rate surge -> $0 revenue

_Staged by the Predictive Pipeline Guardian at 2026-08-05T07:00:48.340627+00:00. Gated — a human approves the merge._

- **Severity:** RED
- **Risk score:** 91/100
- **Predicted lead time:** ~0.0 min
- **Confidence:** 0.74
- **Reasoned by:** `heuristic`

## Evidence
- data-quality (null-rate) rising abnormally (forecast surprise z≈4.7, now 0.95; ~0.0 ticks to its limit)

## Recommended action
Quarantine + repair the malformed records and resolve field aliases before load; the batch is trending toward the null-rate line that zeroes revenue.
