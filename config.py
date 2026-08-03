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
# AI brain — GitHub Models (OpenAI-compatible; the approved org tool)
# ---------------------------------------------------------------------------
GITHUB_MODELS_ENDPOINT = os.environ.get(
    "GITHUB_MODELS_ENDPOINT",
    "https://models.github.ai/inference/chat/completions",
)
GITHUB_MODEL = os.environ.get("GITHUB_MODEL", "openai/gpt-4o-mini")
GITHUB_MODELS_TOKEN = os.environ.get("GITHUB_MODELS_TOKEN", "")
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))

# ---------------------------------------------------------------------------
# AI brain selection — the swappable reasoning layer (see agent/brain.py)
#   BRAIN = copilot | tardis | github_models | auto
#     * copilot / auto -> GitHub Copilot CLI, the approved brain for demos / POCs
#     * tardis         -> LNRS Tardis / Chatflow, the sanctioned production brain
#     * github_models  -> legacy (retired upstream); kept for compatibility only
# ---------------------------------------------------------------------------
BRAIN = os.environ.get("BRAIN", "auto")

# GitHub Copilot CLI brain (demo). The executable is auto-discovered (the VS Code
# Copilot shim, or a copilot on PATH); override only if it lives somewhere unusual.
COPILOT_CLI_PATH = os.environ.get("COPILOT_CLI_PATH", "")
COPILOT_CLI_MODEL = os.environ.get("COPILOT_CLI_MODEL", "cli")  # label for the audit trail
COPILOT_CLI_TIMEOUT_SECONDS = int(os.environ.get("COPILOT_CLI_TIMEOUT_SECONDS", "120"))

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
SLA_LATENCY_MS = float(os.environ.get("SLA_LATENCY_MS", "4000"))

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
