# 🔮 Predictive Pipeline Guardian

> **An AI agent that predicts data-pipeline failures BEFORE they happen — then
> governs the fix through a human.** The proactive counterpart to a reactive
> self-healing system.

It taps a **real open-source real-time feed** (Coinbase public trades), runs a
live **Kafka → Spark → warehouse** ETL on Apache **Airflow**, watches the
pipeline's health signals, and asks a **GitHub Copilot** brain to forecast the
next failure with a **risk score, confidence, lead time, and evidence**. On
risk it raises an early warning, opens a **real predicted-incident GitHub
issue**, and stages a **gated preventive Pull Request** — the AI does the
legwork and *stops*; a human approves the merge.

Every step is written to a **tamper-evident, hash-chained audit trail** and
surfaced on a **live dashboard + Grafana**.

```
EXTRACT  Coinbase live trades ──► Kafka (trades-raw)
   │
TRANSFORM+LOAD  Spark aggregate ──► warehouse  ──► Kafka (trades-aggregated)
   │            (fails when a batch goes mostly-NULL / schema drifts)
SIGNALS  null-rate · volume · schema-hash drift · lag · dup-rate · latency
   │
PREDICT  GitHub Copilot brain  ──►  pipeline_health · risk_score · confidence
   │                                lead_time_minutes · evidence · action
POLICY   GREEN  ·  AMBER (early warning)  ·  RED (imminent failure)
   │
GOVERN   real GitHub issue (AMBER+)  ·  gated preventive PR (RED, human-approved)
   │
AUDIT    hash-chained audit.jsonl  ──►  live dashboard + Prometheus + Grafana + Loki
```

Each Coinbase trade is a real transaction — `price × size` = revenue, `product`
= the market, `time` = the event timestamp, `trade_id` = the idempotency key.

---

## Two ways to run

### 1) Fast path — host demo (no install, no key)

The core loop runs on the **Python standard library only**. It writes to the
same audit / incident files the dashboard reads.

```powershell
cd Agent-AI-Thon

python run_demo.py                                   # healthy live data — stays GREEN
python run_demo.py --inject schema-drift             # agent flags risk BEFORE the ETL breaks
python run_demo.py --inject null-surge --ticks 20 --interval 4
python run_demo.py --inject volume-drop
python run_demo.py --inject latency-surge            # load climbs → latency SLA breach → timeout break
```

The run ends by printing the **LEAD TIME** between the first warning and the
real failure — the headline metric.

**Turn on the Copilot brain** (host only — Copilot CLI needs a VS Code login):

```powershell
$env:BRAIN = "copilot"      # or set BRAIN=copilot in .env
python run_demo.py --inject schema-drift
```

The `AI brain` line then reads `GitHub Copilot` and predictions come from the
model, grounded on the same measured signals.

### 2) Full path — the real Apache stack (Docker)

Brings up **15 containers**: Airflow (LocalExecutor + Postgres), Kafka 3.7
(KRaft), Spark 3.5.1 master+worker, Kafka-UI, an always-on Coinbase→Kafka
stream tap, the live dashboard, Prometheus, Grafana, Loki, Promtail **and an
OpenTelemetry Collector**.

```powershell
cd Agent-AI-Thon
Copy-Item .env.example .env          # fill in GITHUB_TOKEN + GITHUB_REPOSITORY for real governance
cd airflow

# start EVERYTHING (Airflow + observability + Kafka/Spark data plane):
docker compose --env-file ../.env --profile full up -d

# lighter core only (Airflow + dashboard + Grafana/Prometheus/Loki/OTel, no Kafka/Spark):
docker compose --env-file ../.env up -d

# health check / clean re-run:
docker compose --env-file ../.env --profile full ps
docker compose --env-file ../.env --profile full down          # stop
```

> **Ports are offset** so this stack runs **side-by-side** with the sibling
> self-healing (`itsm`) stack without clashing. The `name: agent-aithon` key at
> the top of the compose file scopes every command to *this* project only — it
> never touches the other stack's containers.

| UI | URL | Notes |
|---|---|---|
| **Live dashboard** | http://localhost:18089 | red/amber/green + `/metrics` |
| **Grafana** | http://localhost:13001 | anonymous Admin, 4 pre-provisioned dashboards |
| **Airflow** | http://localhost:18080 | `admin` / `admin` — trigger the DAG here |
| **Prometheus** | http://localhost:19090 | scrapes the dashboard + OTel collector |
| **Kafka-UI** | http://localhost:18085 | watch `trades-raw` / `trades-aggregated` live |
| **Spark** | http://localhost:18081 | master + worker |
| **Loki** | http://localhost:13100 | audit / stream event log (queried by Grafana) |

The four Grafana dashboards (folder **Predictive Guardian**) are: **AI / Agent
Performance**, **ETL / Pipeline Performance**, **OpenTelemetry Service
Performance**, and the original overview — all `refresh: 5s` with live Loki log
panels.

---

## 🎬 Demonstration guide — the judge / SME script

This is problem statement **#4 — AI-Driven Predictive Failure Detection for Data
Pipelines**. The whole point is to **predict the failure BEFORE it happens,
explain why, and govern the fix** — not to react after the pipeline breaks. The
demo below is built around the SME **validation pattern**: *inject a known
failure mode, roll the pipeline forward tick-by-tick, and show that risk was
flagged — with a traceable explanation — ahead of the actual break.*

### A) Watch the data & logs flow (before injecting anything)

Start the full stack, then open these four windows side by side:

1. **Kafka-UI** → http://localhost:18085 — the `trades-raw` topic fills with
   live Coinbase trades every 5s; `trades-aggregated` shows the per-product
   revenue rows the ETL loads. *This is the pipeline actually moving data.*
2. **Airflow** → http://localhost:18080 → DAG **`predictive_pipeline_guardian`**
   → each run shows 4 green tasks: `extract_trades → transform_load →
   predict_risk → govern`. Click any task → **Logs** to read that tick.
3. **Live dashboard** → http://localhost:18089 — the red/amber/green banner +
   the raw `/metrics`.
4. **Grafana** → http://localhost:13001 → *Predictive Guardian* folder — the
   **ETL / Pipeline Performance** board (health, records, null-rate, latency,
   revenue, live throughput) and its two **real-time Loki log panels**: the
   green "heartbeat" (`EXTRACT → TRANSFORM → LOAD`) and the "⚠ Production-issue &
   failure logs" panel (empty while healthy).

**Where the logs physically flow:**

```
Airflow task run ─► agent/audit_trail.py ─► audit/audit.jsonl   (hash-chained decisions)
stream generator ─► audit/stream.jsonl                          (per-cycle heartbeat)
        both files ─► Promtail ─► Loki ─► Grafana log panels     (real-time, 5s refresh)
dashboard /metrics ─► Prometheus ◄─ OTel Collector               ─► Grafana time-series
governor decision  ─► Grafana annotation (red line) + alert rule ─► webhook back to dashboard
```

So a single tick writes a **hash-chained audit event**, a **Loki log line**, a
**Prometheus metric**, and — on risk — a **Grafana annotation + alert**. Every
claim on screen is traceable to a line in `audit/audit.jsonl`.

### B) Inject a production issue → detection kicks in automatically

You do **not** patch code to break the pipeline. You trigger the DAG with a
**run config** and the fault **ramps up each run** — exactly modelling a
real dependency that degrades over time. In the Airflow UI:

**DAG `predictive_pipeline_guardian` → ▶ Trigger DAG w/ config**, paste one of:

```json
{"inject": "schema-drift"}    // upstream renames  price → px   (schema drift)
{"inject": "null-surge"}      // upstream drops the size field  → NULL revenue
{"inject": "volume-drop"}     // upstream stalls / starves the batch
{"inject": "latency-surge"}   // load climbs → latency SLA breach → processing timeout
{"reset": true}               // clear the ramp + incident banner
{}                            // healthy live data → stays GREEN
```

Then **trigger it 3–5 times in a row** (or let the `*/10 min` schedule run). The
ramp counter (`guardian_state.json`) makes each run worse than the last, so you
literally watch risk climb **GREEN → AMBER → RED** across the ticks — while the
ETL is *still succeeding*. That gap is the headline metric: **lead time**.

### C) What the agent does on each injected tick (detection → RCA → govern)

| Task | What happens | Where you see it |
|---|---|---|
| `extract_trades` | Live batch pulled, ramping fault applied | Airflow log: `mode`, `tick` |
| `transform_load` | ETL runs; health signals computed (null-rate, schema-hash drift, volume, latency) and persisted to a rolling window | ETL/Pipeline Grafana board; `etl_run` audit event |
| `predict_risk` | The **AI brain** forecasts `risk_score`, `confidence`, `lead_time_minutes`, `predicted_failure_type` and **evidence** (the contributing signals) | AI/Agent Grafana board; `agent_reason` audit event |
| `govern` | Policy grades GREEN/AMBER/RED → raises the early warning → **opens a REAL GitHub issue (AMBER+)** and stages a **gated preventive PR (RED)** → **declares a Grafana incident** (a tagged annotation on every board + a real **Grafana IRM incident** when `GRAFANA_IRM_TOKEN` is set) + fires the **alert** | Grafana incident + annotation + alert; `prediction` / `grafana_incident_opened` audit events; GitHub |

**The "root-cause / explanation" (Explainable Risk Insights):** the prediction
carries an `evidence` list (e.g. *"schema hash changed vs baseline; `price`
column missing; null-rate 0.0 → rising"*), a `predicted_failure_type`
(`schema_drift` / `data_quality` / `starvation` / `latency-degradation`), and a
`recommendation`. That
same evidence is written verbatim into the GitHub **issue body** and the
preventive **PR runbook** — so the stated driver is traceable straight back to
the measured signal, which is exactly what the SMEs spot-check.

### D) Run all the scenarios (don't demo just one)

The SME validation explicitly replays **multiple, unseen** incident types, so
show the breadth — reset between runs for a clean lead-time story:

```jsonc
// 1. Schema drift  — upstream renames a column, downstream silently breaks
{"inject": "schema-drift"}   ×4      then   {"reset": true}

// 2. Data-quality / null surge — a field goes missing, revenue reads as NULL
{"inject": "null-surge"}     ×4      then   {"reset": true}

// 3. Volume drop / starvation — upstream stalls, batch runs thin
{"inject": "volume-drop"}    ×4      then   {"reset": true}

// 4. Latency / load surge — load climbs, processing latency breaches the SLA,
//    then a hard timeout BREAKS the pipeline. The remediation is a CODE change
//    (scale consumers + backpressure + batch chunking) staged in the PR runbook.
{"inject": "latency-surge"}  ×6      then   {"reset": true}
```

For each: point at the **AMBER tick** (agent warned) → the later **RED tick**
(imminent) → and note the ETL only actually failed *after* the warnings. Read
the **lead time** off the banner / audit trail. `run_demo.py` prints this
number directly on the host path.

### E) How it satisfies the problem statement

| Required pillar | In this demo |
|---|---|
| **Pipeline Health Monitoring** | Consolidated health view (dashboard + 4 Grafana boards): executions, latency, throughput, null-rate, schema-hash, volume, SLA-style signals |
| **Predictive Failure Detection** | Risk score + confidence + lead time per tick, ramping across runs, flagged **before** the ETL fails |
| **Explainable Risk Insights** | `evidence` list + `predicted_failure_type` correlated from logs/quality/latency, echoed into the issue & PR — traceable to the raw signal |
| **Preventive Actions** *(extension)* | Real predicted-incident **GitHub issue** (AMBER+), a **gated preventive PR** with a runbook (RED), and a **Grafana incident** (IRM + tagged annotation) carrying the same RCA + remediation — AI does the legwork, a human approves the merge |

| Success metric (judging) | How to show it |
|---|---|
| **Detection accuracy** | Injected modes flag risk; healthy `{}` stays GREEN (no false alarms) |
| **Lead time** | The GREEN→AMBER→RED gap before the real failure, printed by `run_demo.py` |
| **Explainability** | Open the GitHub issue / PR — its drivers match the audit `evidence` |
| **Confidence calibration** | `confidence` rises with signal strength; borderline ticks report lower confidence |
| **Operational value** | The governed issue/PR is the "earlier, explained warning changes what the team does" moment |

| Expected business outcome | Delivered by |
|---|---|
| Fewer failures via earlier detection | AMBER/RED warning + preventive PR *before* downstream impact |
| Better data reliability / freshness | Continuous health monitoring + SLA-style signals |
| Less manual triage | Auto-opened, de-duplicated, evidence-filled incident tickets |
| Proactive intervention | Human-approved gated fix staged while the pipeline is still green |
| Auditable resilience | Tamper-evident hash-chained `audit.jsonl` (`verify_chain`) |

> **Tamper-evidence proof:** the audit trail is hash-chained — edit any past
> line in `audit/audit.jsonl` and `verify_chain` (and the dashboard's
> `guardian_audit_intact` metric → Grafana alert) flips to broken. Nothing the
> agent claims can be silently rewritten.

---


## The AI brain is swappable

`BRAIN` selects the reasoning engine — the rest of the system is unchanged:

| `BRAIN` | Where | Behaviour |
|---|---|---|
| `copilot` | host / demo | **GitHub Copilot CLI** (needs a VS Code Copilot login) |
| `tardis` | production | in-cluster model seam (runs headless in containers) |
| `auto` *(default)* | anywhere | try Copilot, else a transparent **heuristic** — safe in containers |

> **Why the split?** The Copilot CLI can't authenticate headlessly, so *inside*
> the containers the agent falls back to the transparent heuristic (or `tardis`,
> the production brain). The **Copilot** brain is demonstrated on the **host**
> via `run_demo.py`, writing to the same mounted audit/incident files the
> containerised dashboard reads. Same data, same governance, one brain swap.

---

## Governance — the AI stops at the gate

When `GITHUB_TOKEN` + `GITHUB_REPOSITORY` are set (see `.env.example`):

- **AMBER+** → opens a **real GitHub issue** labelled `predicted-incident`,
  de-duplicated per predicted-failure signature.
- **RED** → pushes a branch `guardian/prevent-<sig>`, commits a prevention
  runbook, and opens a **gated Pull Request** against the default branch. It is
  **never auto-merged** — a human approves. Also de-duplicated per signature.
- **AMBER+** → also **declares a Grafana incident** carrying the same AI analysis
  (RCA evidence + code remediation + links to the issue / PR): a tagged
  annotation on every dashboard (the OSS-Grafana marker), plus a real **Grafana
  IRM incident** when `GRAFANA_IRM_TOKEN` is set. De-duplicated per signature and
  **auto-resolved** when the pipeline returns to GREEN.

With no token it degrades to a printed, fully-audited plan — nothing silently
fails.

---

## Layout

| Path | Role |
|---|---|
| `ingestion/coinbase_source.py` | Extract — live trades from Coinbase |
| `pipeline/etl.py` | Transform + Load (with the latent load-fail condition) |
| `pipeline/kafka_io.py` | Kafka producer/consumer (Coinbase fallback when Kafka is down) |
| `signals/collector.py` | Health-signal / feature store |
| `agent/brain.py` · `copilot_cli.py` | Swappable AI brain (Copilot / Tardis / heuristic) |
| `agent/predictive_agent.py` | The agentic loop: perceive → recall → ground → reason → learn |
| `agent/vector_memory.py` | Incident memory (RAG, grows over time) |
| `agent/audit_trail.py` | Tamper-evident **hash-chained** audit + `verify_chain` |
| `policy/policy_engine.py` | GREEN / AMBER / RED decision |
| `governance/github_gov.py` | **Real** predicted-incident issue + gated preventive PR |
| `alerting/notifier.py` | Early warning → audit → governance → incident banner |
| `dashboard.py` | Live red/amber/green monitor + Prometheus `/metrics` |
| `faults.py` | Demo fault injection (ramping) |
| `run_demo.py` | End-to-end host runner + lead-time report |
| `airflow/dags/predictive_guardian_dag.py` | The Airflow DAG (extract → load → predict → govern) |
| `airflow/stream_generator.py` | Always-on Coinbase → Kafka tap |
| `airflow/docker-compose.yaml` | The 15-container stack |
| `airflow/monitoring/*` | Prometheus, Loki, Promtail, Grafana provisioning + dashboard |

See **[RUNBOOK.md](RUNBOOK.md)** for the full demo script.