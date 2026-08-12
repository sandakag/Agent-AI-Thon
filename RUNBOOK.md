# RUNBOOK — Predictive Pipeline Guardian demo

A tight, repeatable script for demoing the guardian. Two tracks: a **90-second
host demo** (zero setup) and the **full Docker stack** (real Apache + Grafana +
real GitHub governance).

---

## Track A — 90-second host demo (no install, no key)

> Shows the whole predict → warn → govern → audit loop, ending with the headline
> **lead-time** number. Uses the Copilot brain if you have a VS Code Copilot
> login; otherwise the transparent heuristic.

```powershell
cd Agent-AI-Thon

# (optional) turn on the Copilot brain
$env:BRAIN = "copilot"

# 1) Baseline — live Coinbase data stays GREEN
python run_demo.py

# 2) The money shot — the agent predicts the failure BEFORE the ETL breaks
python run_demo.py --inject schema-drift
```

**What to point at**

1. The `AI brain` line — `GitHub Copilot` (or `heuristic`).
2. The stream flipping GREEN → **AMBER** several ticks *before* the ETL fails.
3. The final **LEAD TIME** line — minutes of warning bought.
4. `data/active_incidents.json` + `audit/audit.jsonl` — the banner and the
   hash-chained trail the dashboard reads.

Other faults: `--inject null-surge --ticks 20 --interval 4`, `--inject volume-drop`, `--inject latency-surge` (load climbs → latency SLA breach → timeout break; remediation is a code fix).

---

## Track B — full Apache stack + real governance

### 0) One-time setup

```powershell
cd Agent-AI-Thon
Copy-Item .env.example .env
```

Edit `.env`:

- `GITHUB_TOKEN` — fine-grained PAT with **Contents / Issues / Pull requests =
  Read & Write** on the target repo.
- `GITHUB_REPOSITORY` — `your-user/your-repo`.
- Leave `BRAIN=auto` for containers (Copilot can't auth headlessly).

### 1) Bring the stack up

```powershell
cd airflow
./stack_up.sh          # first boot builds the Airflow image (a few minutes)
```

Open the tabs:

| UI | URL |
|---|---|
| Live dashboard | http://localhost:8089 |
| Grafana | http://localhost:3001 |
| Airflow | http://localhost:8080 (`admin`/`admin`) |
| Kafka-UI | http://localhost:8085 |
| Prometheus | http://localhost:9090 |
| Spark | http://localhost:8081 |

Wait until the dashboard shows a **GREEN** banner and Kafka-UI shows
`trades-raw` / `trades-aggregated` filling (the stream tap is always on).

### 2) Trigger a predicted incident

In Airflow, open **`predictive_pipeline_guardian`** → **Trigger DAG w/ config**:

```json
{"inject":"schema-drift"}
```

Watch, in order:

1. **Dashboard** flips **AMBER** then **RED**; KPI cards show rising risk and a
   falling lead time; the prediction stream fills.
2. **Grafana** (Predictive Pipeline Guardian dashboard) — the risk/health
   timeseries diverge; the Loki panel shows `prediction` →
   `governance_issue_opened` → `governance_pr_opened` events.
3. **GitHub** — a new `predicted-incident` **issue**, and (on RED) a
   `guardian/prevent-*` branch with a **gated PR** awaiting your approval.

### 3) Prove the governance gate

Open the PR — note it is **not merged**. The AI staged the prevention runbook
and stopped; **you** approve. This is the human-in-the-loop control.

### 4) Prove the audit is tamper-evident

The dashboard's **Audit chain** card reads `INTACT`. Hand-edit any line in
`audit/audit.jsonl` and refresh — it flips to `BROKEN at #<n>`.

### 5) Reset for the next run

```powershell
cd airflow
./reset-demo.ps1            # clears audit/banner/state, keeps metrics history
./reset-demo.ps1 -Hard     # also wipes Grafana/Prometheus volumes (fresh)
```

---

## Track C — repeatable post-production self-healing demonstration

This track uses one **controlled demo fault**, not a real security vulnerability:
the parser temporarily stops accepting the upstream field alias `quantity` for
`size`. It is intentionally not covered by the small CI suite, so CI is green
and the defect reaches the demo production stack. The live incident then proves
the separate, post-production guardian path.

### 0) One-time prerequisites

- Complete Track B configuration, including `GITHUB_TOKEN` and
  `GITHUB_REPOSITORY` in `.env`.
- Set `GOVERNANCE_REQUIRE_APPROVAL=true` in `.env` to make the approval button
  visible. Restart the Docker stack after changing `.env`.
- Use an isolated demo repository/branch where you are permitted to push the
  intentional demonstration commit to `main`.

### 1) Deploy the controlled fault (one command)

```powershell
python scripts/demo_vulnerability.py enable
python scripts/demo_vulnerability.py status       # prints: DEMO FAULT ENABLED
python -m unittest discover -s tests -v            # stays green by design
git add pipeline/etl.py
git commit -m "demo: introduce controlled quantity-alias fault"
git push origin main

# Restart the local production-demo stack so guardian-loop imports the deployed commit.
cd airflow
./stack_up.sh
cd ..
```

The script changes only one known line and does **not** commit, push, or contact
GitHub. Review `git diff` before committing.

### 2) Induce the production incident

1. Open the live dashboard.
2. Under **Induce your OWN incident**, click **Rename size → quantity**. It only
   loads the preset; click **Induce custom incident** to fire it.
3. Watch the live pipeline progress **GREEN → AMBER → RED**. At RED, click
   **Approve & file governed issue / PR**.
4. GitHub Copilot receives the incident telemetry and current ETL source, then
   authors a bounded `pipeline/etl.py` repair. GitHub receives the resulting
   issue and `guardian/fix-*` PR. Review and merge it yourself.

### 3) Show automatic recovery after your merge

Within `GUARDIAN_MERGE_POLL_TICKS × GUARDIAN_LOOP_INTERVAL` (normally about 12
seconds), the guardian detects the merged PR, hot-reloads `pipeline/etl.py`, and
starts recovery. In the next `GUARDIAN_RECOVERY_TICKS` ticks it clears the
incident and returns to **GREEN**. Point to the dashboard banner and the
`guardian_fix_merged` / `guardian_fix_recovered` events in the logs.

### 4) Prepare for the next audience

After the fix is merged, repeat Step 1. `enable` is idempotent: it deliberately
reintroduces only this controlled demo fault, so every session starts fresh.
If you need to abandon a run before merging, restore your local source with:

```powershell
python scripts/demo_vulnerability.py disable
```

---

## Talking points

- **Proactive, not reactive** — the agent forecasts and *prevents*; the reactive
  engine is only the safety net, and every miss trains the vector memory.
- **Swappable brain** — Copilot on the host, Tardis in production, heuristic
  everywhere as a transparent fallback. One env var, no code change.
- **Governed autonomy** — real GitHub issues + **gated** PRs, de-duplicated per
  failure signature. The AI never merges.
- **Provable trust** — a hash-chained audit trail you can verify live, streamed
  to Loki and visualised in Grafana next to the Prometheus KPIs.
- **Real everything** — real Coinbase data, real Kafka/Spark/Airflow, real
  Prometheus/Grafana/Loki. No mocks in the data plane.
