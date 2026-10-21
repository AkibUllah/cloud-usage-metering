#!/usr/bin/env bash
# Codespaces start hook: bring up API + PostgreSQL and seed demo usage with the simulator.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d --build

echo "waiting for the API on :8000 ..."
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# Deterministic sample ids make this idempotent: re-runs report duplicates and regenerate
# the same invoices, so it is safe on every Codespace start.
docker compose exec -T api python scripts/simulate_usage.py \
  --base-url http://localhost:8000 --days 3 --seed 42 --invoice

cat <<MSG

Cloud Usage Metering is up.
  API docs:   forwarded port 8000, path /docs
  Try:        GET /v1/tenants, GET /v1/tenants/{id}/usage/summary?period=$(date -u +%Y-%m), GET /v1/invoices
MSG
