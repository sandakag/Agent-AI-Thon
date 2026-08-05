"""Central configuration for the Predictive Pipeline Guardian.

Every secret / environment-specific value is read from the environment or a
local ``.env`` file — nothing is hard-coded. Sensible defaults keep the demo
runnable offline: when no GitHub Models token is present, the predictive agent
degrades gracefully to a transparent heuristic, so the whole loop still runs.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env_file(env_path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Real environment values win (setdefault). Obvious placeholders such as
    ``PASTE_...`` / ``YOUR_...`` / ``<...>`` are ignored so a placeholder token
    never masks the fact that no real secret has been provided yet.
    """
    env_file = env_path or (ROOT / ".env")
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and not value.upper().startswith(("PASTE", "YOUR_", "<")):
            os.environ.setdefault(key, value)


load_env_file()

# ---------------------------------------------------------------------------
# Paths (created on first run)
# ---------------------------------------------------------------------------
DATA_DIR = ROOT / "data"
AUDIT_DIR = ROOT / "audit"
DATA_DIR.mkdir(exist_ok=True)
AUDIT_DIR.mkdir(exist_ok=True)

WAREHOUSE_FILE = DATA_DIR / "warehouse.json"       # ETL destination (the "warehouse")
MEMORY_FILE = DATA_DIR / "vector_memory.json"      # agent's incident memory (RAG)
INCIDENTS_FILE = DATA_DIR / "active_incidents.json"  # live red/amber banner state
AUDIT_LOG = AUDIT_DIR / "audit.jsonl"              # append-only, hash-chained trail
STREAM_LOG = AUDIT_DIR / "stream.jsonl"            # lock-free live heartbeat (Promtail->Loki)
SIGNAL_HISTORY_FILE = DATA_DIR / "signal_history.json"  # rolling signal window (cross-run trends)
GUARDIAN_STATE_FILE = DATA_DIR / "guardian_state.json"  # per-mode tick counter for the ramp
HISTORY_DB = str(DATA_DIR / "guardian_history.db")      # optional SQLite history

# ---------------------------------------------------------------------------
# Real-time source — Coinbase public trades API (free, NO API key)
#   GET {base}/products/{product}/trades?limit=N
# ---------------------------------------------------------------------------
COINBASE_BASE = os.environ.get("COINBASE_BASE", "https://api.exchange.coinbase.com")
PRODUCTS = [
    p.strip()
    for p in os.environ.get(
        "PRODUCTS", "BTC-USD,ETH-USD,SOL-USD,XRP-USD,DOGE-USD,ADA-USD"
    ).split(",")
    if p.strip()
]
TRADES_PER_PRODUCT = int(os.environ.get("TRADES_PER_PRODUCT", "50"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))
USER_AGENT = os.environ.get("USER_AGENT", "PredictivePipelineGuardian/1.0")

# ---------------------------------------------------------------------------
# Real data plane — Apache Kafka (the live trades bus, when the Docker stack is up)
#   trades-raw       <- live Coinbase trades (stream-generator publishes)
#   trades-aggregated<- revenue-per-product rows (the ETL sink)
# Outside Docker the Kafka I/O falls back to a direct Coinbase fetch, so the
# guardian still runs end-to-end with `python run_demo.py`.
# ---------------------------------------------------------------------------
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
KAFKA_TOPIC_RAW = os.environ.get("KAFKA_TOPIC_RAW", "trades-raw")
KAFKA_TOPIC_AGG = os.environ.get("KAFKA_TOPIC_AGG", "trades-aggregated")

# ---------------------------------------------------------------------------
# Observability — live dashboard (HTML + Prometheus /metrics) and Grafana
# ---------------------------------------------------------------------------
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8089"))
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3001")
GRAFANA_USER = os.environ.get("GRAFANA_USER", "admin")
GRAFANA_PASSWORD = os.environ.get("GRAFANA_PASSWORD", "admin")

# OpenTelemetry — optional OTLP export (traces + metrics) to the collector. The
# collector always scrapes the dashboard /metrics endpoint, so the OTel service
# dashboard has live data even when the Python OTLP SDK isn't installed; these
# knobs simply light up the richer per-cycle app telemetry after an image build.
OTEL_ENABLED = os.environ.get("OTEL_ENABLED", "true").lower() in ("1", "true", "yes")
OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "predictive-guardian")

# ---------------------------------------------------------------------------
# AI brain selection — the swappable reasoning layer (see agent/brain.py)
#   BRAIN = copilot | tardis | auto
#     * copilot / auto -> GitHub Copilot CLI, the approved reasoning brain (demo)
#     * tardis         -> LNRS Tardis / Chatflow, the sanctioned production brain
# The Copilot brain authenticates with its own CLI login (your Copilot seat);
# there is no model token or endpoint to configure.
# ---------------------------------------------------------------------------
BRAIN = os.environ.get("BRAIN", "auto")

# GitHub Copilot CLI brain (demo). The executable is auto-discovered (the VS Code
# Copilot shim, or a copilot on PATH); override only if it lives somewhere unusual.
COPILOT_CLI_PATH = os.environ.get("COPILOT_CLI_PATH", "")
# Preferred model the brain requests via `copilot --model`. Use "auto" to let
# Copilot pick the best model your seat allows (works on every plan and keeps the
# demo fast). Set a specific id -- e.g. "claude-opus-4.8" -- to force a bigger
# model, but note the premium models (Opus / Sonnet / GPT-5.x / Gemini Pro)
# require a Copilot Pro+/Business/Enterprise/Max seat. If the preferred model is
# not on your seat, the brain logs a warning and falls back to
# COPILOT_CLI_FALLBACK_MODEL, so the agent never breaks.
COPILOT_CLI_MODEL = os.environ.get("COPILOT_CLI_MODEL", "auto")
COPILOT_CLI_FALLBACK_MODEL = os.environ.get("COPILOT_CLI_FALLBACK_MODEL", "auto")
# Optional reasoning depth for richer, more detailed answers. One of:
# none | minimal | low | medium | high | xhigh | max. Empty = model default.
COPILOT_CLI_REASONING_EFFORT = os.environ.get("COPILOT_CLI_REASONING_EFFORT", "")
COPILOT_CLI_TIMEOUT_SECONDS = int(os.environ.get("COPILOT_CLI_TIMEOUT_SECONDS", "120"))

# GitHub Copilot REST API brain (Claude Opus 4.8) — the high-power reasoning
# brain used for the detailed RCA and the operator chat. It talks to the Copilot
# Chat API directly (like the VS Code extension) using your existing Copilot
# subscription, so it can force a premium model and a large output budget that
# the CLI's `--model` flag can't on some seats. Auth needs no PAT/API key — a
# GitHub Copilot OAuth token is discovered from the environment, a token file, or
# the official Copilot plugin config (see agent/copilot_api.py).
COPILOT_MODEL = os.environ.get("COPILOT_MODEL", "claude-opus-4.8")
# Output budget for a single completion (Opus supports a large context; this caps
# how long ONE answer/RCA can be). Set 0 to let the service pick its default.
COPILOT_MAX_TOKENS = int(os.environ.get("COPILOT_MAX_TOKENS", "8000"))
COPILOT_API_TIMEOUT = int(os.environ.get("COPILOT_API_TIMEOUT", "120"))
# Optional explicit path to a Copilot OAuth token file ({"access_token": "..."}).
# Used to hand the host's subscription token to the headless Docker containers.
COPILOT_TOKEN_STORE = os.environ.get("COPILOT_TOKEN_STORE", "")


# Tardis / Chatflow brain (production seam).
TARDIS_MODEL = os.environ.get("TARDIS_MODEL", "chatflow")

# ---------------------------------------------------------------------------
# Pipeline health thresholds
# ---------------------------------------------------------------------------
# The load stage refuses to publish (fails) when the batch is mostly NULL
# amounts — publishing would silently report $0 revenue. This is the incident
# the agent must PREDICT before it fires.
NULL_RATE_CRITICAL = float(os.environ.get("NULL_RATE_CRITICAL", "0.60"))
MIN_RECORDS = int(os.environ.get("MIN_RECORDS", "20"))          # below = starvation
SLA_LATENCY_MS = float(os.environ.get("SLA_LATENCY_MS", "4000"))     # soft SLA (early warn)
# Under sustained load the effective processing latency climbs; once it crosses
# this HARD ceiling the load stage aborts the batch (a processing timeout) — the
# "pipeline broke under load" incident the agent must PREDICT from the rising
# latency trend, well before it actually fires.
LATENCY_TIMEOUT_MS = float(os.environ.get("LATENCY_TIMEOUT_MS", "9000"))

# ---------------------------------------------------------------------------
# Risk bands used by the preventive policy engine
# ---------------------------------------------------------------------------
RISK_AMBER = float(os.environ.get("RISK_AMBER", "40"))   # early warning
RISK_RED = float(os.environ.get("RISK_RED", "70"))       # imminent / firing

# ---------------------------------------------------------------------------
# Optional governance (predicted-incident issue / gated PR). Used only if set.
# ---------------------------------------------------------------------------
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# ---------------------------------------------------------------------------
# Optional Grafana IRM / Incident governance (a REAL incident on the predicted
# failure). The stack's Grafana is OSS, so the incident is ALWAYS surfaced as a
# tagged annotation on every board (the "Guardian incidents" marker). When a
# Grafana IRM service-account token is provided, the SAME predicted-failure
# analysis is ALSO declared as a real incident via the Grafana Incident API.
# ---------------------------------------------------------------------------
GRAFANA_IRM_URL = os.environ.get("GRAFANA_IRM_URL", GRAFANA_URL)
GRAFANA_IRM_TOKEN = os.environ.get(
    "GRAFANA_IRM_TOKEN", os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "")
)
