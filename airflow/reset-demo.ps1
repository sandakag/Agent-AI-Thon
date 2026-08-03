<#
  reset-demo.ps1 — one-command reset for the Predictive Pipeline Guardian demo.

  Tears the full stack down (optionally wiping volumes), clears the local audit
  trail / incident banner / warehouse state, then brings everything back up so
  every run starts from a clean, reproducible GREEN state.

  Usage:
    ./reset-demo.ps1            # soft reset (keep Grafana/Prometheus history)
    ./reset-demo.ps1 -Hard      # wipe named volumes too (fresh metrics)
#>
param([switch]$Hard)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$envFile = "../.env"
if (-not (Test-Path $envFile)) { $envFile = "../.env.example" }

Write-Host "Stopping stack..." -ForegroundColor Cyan
if ($Hard) {
    docker compose --env-file $envFile --profile full down --remove-orphans --volumes
} else {
    docker compose --env-file $envFile --profile full down --remove-orphans
}

Write-Host "Clearing local guardian state..." -ForegroundColor Cyan
$audit = "../audit"
$data  = "../data"
foreach ($f in @("$audit/audit.jsonl", "$audit/stream.jsonl")) {
    if (Test-Path $f) { Clear-Content $f }
}
foreach ($f in @("$data/active_incidents.json", "$data/guardian_state.json",
                 "$data/signal_history.json")) {
    if (Test-Path $f) { Remove-Item $f -Force }
}
'{ "level": "GREEN" }' | Set-Content "$data/active_incidents.json"

Write-Host "Bringing stack back up..." -ForegroundColor Cyan
docker compose --env-file $envFile --profile full up -d --build

Write-Host ""
Write-Host "Reset complete. UIs:" -ForegroundColor Green
Write-Host "  Airflow    http://localhost:8080  (admin/admin)"
Write-Host "  Dashboard  http://localhost:8089"
Write-Host "  Grafana    http://localhost:3001"
Write-Host "  Prometheus http://localhost:9090"
Write-Host "  Kafka-UI   http://localhost:8085"
Write-Host "  Spark      http://localhost:8081"
