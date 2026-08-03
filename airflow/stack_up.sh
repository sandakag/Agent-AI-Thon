#!/usr/bin/env bash
# Bring the whole real Apache stack up for the Predictive Pipeline Guardian:
# Airflow + Kafka + Spark + Kafka-UI + Prometheus + Grafana + Loki + Promtail +
# the live dashboard and the always-on Coinbase stream tap.
#
# Usage:  ./stack_up.sh
set -e
cd "$(dirname "$0")"

ENV_FILE="../.env"
[ -f "$ENV_FILE" ] || ENV_FILE="../.env.example"

docker compose --env-file "$ENV_FILE" --profile full down --remove-orphans 2>/dev/null || true
docker compose --env-file "$ENV_FILE" --profile full up -d --build

echo ""
echo "Predictive Pipeline Guardian is starting. UIs:"
echo "  Airflow    http://localhost:8080  (admin/admin)"
echo "  Dashboard  http://localhost:8089  (red/amber/green + /metrics)"
echo "  Grafana    http://localhost:3001  (anonymous Admin)"
echo "  Prometheus http://localhost:9090"
echo "  Kafka-UI   http://localhost:8085"
echo "  Spark      http://localhost:8081"
echo ""
echo "Trigger a fault from Airflow (DAG: predictive_pipeline_guardian) with conf:"
echo '  {"inject":"schema-drift"}   {"inject":"null-surge"}   {"inject":"volume-drop"}   {"inject":"latency-surge"}'
