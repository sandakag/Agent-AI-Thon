# Predictive Pipeline Guardian — Production Scenarios

Six realistic, customer‑grade scenarios the Guardian catches **before** they hit
production. In each one the AI does the anomaly detection, predicts the failure
**with lead time while the pipeline is still green**, and hands the team a clear,
actionable plan — *what the issue is, how it was detected early, the exact steps
to run now, and whether a code change (pull request) is needed*. A human stays in
control: the AI files **nothing** until you click **Approve** (or **Dismiss** and
fix it yourself).

> The point is prevention, not post‑mortems. The Guardian raises the alarm on the
> *trend* — typically minutes before the breach — so the team resolves it before a
> single customer is affected.

Each scenario below can be reproduced live from the dashboard
(`http://localhost:18089`): click the preset to **load** it into the box, then
**Induce custom incident**. Watch GREEN → AMBER (early warning) → RED (imminent),
read the AI analysis, then **Approve** or **Resolve**.

---

## 1. Latency SLA‑breach rate rising (the "95% completion SLO" scenario)

- **Business promise at risk:** "95% of transactions complete within the SLA."
- **Normal vs incident:** a few slow transactions in a 5–10 min window is normal
  market noise. The incident is a **sustained rise in the *rate* of slow
  transactions** — the processing‑latency trend climbing tick over tick.
- **What the AI detects early:** the latency trend crossing toward the SLA and the
  breach‑rate climbing — flagged after a few consecutive ticks, **not** after
  waiting the 5–6 minutes it would take customers to feel it. Typical lead time
  **~1–3 min** before the 95% SLO breaches / the hard timeout aborts the batch.
- **Predicted impact if ignored:** SLA breach → hard processing timeout → the load
  stage aborts the batch → downstream revenue/aggregates stall.
- **Recommended actions (handed over before it breaks):**
  1. Scale out ETL consumers / raise parallelism now.
  2. Enable backpressure and chunk the batch to cap per‑batch work.
  3. Shed or defer non‑critical load until latency is back under the SLA.
  4. Check the processing tier for saturation (CPU, GC, connection‑pool, downstream sink latency).
- **Code fix needed?** Usually **no** — operational (scale/backpressure/config). The
  issue carries the steps; no PR.
- **Reproduce:** preset **Add latency** → `[{"op":"latency","ms":800}]`.

## 2. Data‑quality / null‑rate surge → $0 revenue

- **Business promise at risk:** accurate revenue/settlement figures.
- **Normal vs incident:** an occasional missing field is tolerable; the incident is
  the **null / malformed‑record rate climbing abnormally** as an upstream producer
  degrades.
- **What the AI detects early:** the null‑rate trend rising well past its learned
  baseline and heading for the load gate that zeroes revenue — lead time
  **~1–2 min** before the gate trips.
- **Predicted impact if ignored:** revenue silently under‑reported, then dropped to
  **$0** at the load gate; corrupt figures downstream.
- **Recommended actions:**
  1. Quarantine + repair the malformed records before load.
  2. Resolve field aliases / defaults for the affected fields.
  3. Hold the load if the null‑rate approaches the critical gate.
- **Code fix needed?** Often **yes** — add producer‑side / parser validation. The
  Guardian stages a **gated PR** with the fix; a human reviews and merges.
- **Reproduce:** preset **Null size** → `[{"op":"null_field","field":"size"}]`.

## 3. Schema drift (upstream field rename) → parse failures / dropped rows

- **Business promise at risk:** complete, correct data capture.
- **Normal vs incident:** stable schema; the incident is an **upstream field
  renamed/reshaped**, so the strict parser starts silently dropping rows.
- **What the AI detects early:** the record schema‑hash diverging from the learned
  baseline and parse failures climbing **before** rows are silently lost.
- **Predicted impact if ignored:** records dropped → volume and revenue fall →
  incomplete aggregates.
- **Recommended actions:**
  1. Add the renamed field alias to the parser.
  2. Quarantine unparseable records; reprocess after the alias lands.
- **Code fix needed?** **Yes** — parser/schema‑contract change. Guardian stages a
  **gated PR**.
- **Reproduce:** preset **Rename price** → `[{"op":"rename_field","field":"price","to":"px"}]`.

## 4. Volume collapse / throughput starvation → stale aggregates

- **Business promise at risk:** fresh, timely dashboards and downstream feeds.
- **Normal vs incident:** batch volume naturally varies; the incident is the batch
  **shrinking tick over tick** — an upstream producer stall or consumer lag.
- **What the AI detects early:** record volume trending below its normal band toward
  the starvation line — lead time before downstream goes stale.
- **Predicted impact if ignored:** downstream aggregates/revenue become stale or
  incomplete; SLAs on freshness breach.
- **Recommended actions:**
  1. Check upstream producer / consumer lag.
  2. Scale consumers and backfill the affected window.
- **Code fix needed?** Usually **no** — operational (scale/backfill). Steps in the issue.
- **Reproduce:** preset **Shrink batch** → `[{"op":"shrink_batch"}]`.

## 5. Duplicate storm (at‑least‑once redelivery) → double‑counted revenue

- **Business promise at risk:** exactly‑once accuracy of counts and revenue.
- **Normal vs incident:** rare duplicates are expected; the incident is a
  **redelivery storm** (a consumer/offset reset) inflating the duplicate rate.
- **What the AI detects early:** the duplicate‑rate climbing abnormally vs baseline
  before totals are visibly wrong.
- **Predicted impact if ignored:** inflated counts and **double‑counted revenue**;
  reconciliation pain.
- **Recommended actions:**
  1. Enable idempotency / dedup keys on the consumer.
  2. Fix the offset‑commit / checkpoint that caused the replay.
  3. Reconcile the affected window.
- **Code fix needed?** **Yes** — idempotency/offset handling. Guardian stages a **gated PR**.
- **Reproduce:** preset **Dup storm** → `[{"op":"duplicate"}]`.

## 6. Stale / frozen feed → decisions on stale data

- **Business promise at risk:** decisions made on **fresh** data.
- **Normal vs incident:** minor freshness jitter is fine; the incident is a feed that
  **stops updating** (values frozen), so freshness/lag grows.
- **What the AI detects early:** the freshness‑lag trend (or a frozen, constant
  signal) drifting past its learned normal — lead time before decisions run on
  stale data.
- **Predicted impact if ignored:** pricing/decisions on stale inputs; downstream
  correctness and trust erode.
- **Recommended actions:**
  1. Fail over to the backup feed / cache.
  2. Reconnect the primary with backoff; alert on staleness.
- **Code fix needed?** Usually **no** — operational (failover/reconnect). Steps in the issue.
- **Reproduce:** preset **Freeze price** → `[{"op":"freeze_field","field":"price"}]`.

---

## Bonus scenarios (same engine)

- **Source outage / connection errors** — extraction errors rising → predict a gap;
  action: fail over / circuit‑breaker / backoff (manual).
- **Outlier / bad‑value spike** — a field value spikes (e.g. price ×50) → skewed
  aggregates; action: add bounds/validation, quarantine (code).

## What every incident produces

For **each** scenario the Guardian delivers the same package — on the dashboard
**and** (on approval) in the GitHub issue / gated PR:

1. **What the issue is** and its **root cause**.
2. **How the AI detected it early** — the anomalous pattern + the lead time it bought.
3. **Predicted impact** if it is not acted on.
4. **The exact manual steps to run NOW** to prevent it.
5. **Whether a code fix is needed** — if so, a **gated pull request** is staged for a
   human to review and merge; if not, the operational steps are all that is required.
6. **Preventive measures** so it does not recur.

Nothing is merged or auto‑fixed without a human. The AI does the detection and the
legwork; you approve and act.
