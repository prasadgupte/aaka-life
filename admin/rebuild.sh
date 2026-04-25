#!/usr/bin/env bash
# admin/rebuild.sh — Quick VPS redeploy (tear down, pull, rebuild, smoke test)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/detect.sh"

header "Rebuild (VPS)"
[[ "$INSTANCE" != "vps" ]] && { fail "rebuild.sh must run on VPS"; exit 1; }

info "Tearing down containers..."
docker compose -f docker-compose.prod.yml down --remove-orphans

info "Pulling latest code..."
git pull --ff-only

info "Building and starting sensor..."
docker compose -f docker-compose.prod.yml up -d --build --force-recreate sensor

info "Smoke tests..."
docker compose -f docker-compose.prod.yml ps sensor
docker compose -f docker-compose.prod.yml logs --tail=20 sensor
docker compose -f docker-compose.prod.yml exec sensor \
  python3 -c "from gateway.adapter import GatewayAdapter; print('gateway OK')"
docker compose -f docker-compose.prod.yml exec sensor \
  python3 aaka_config.py
docker compose -f docker-compose.prod.yml exec sensor \
  python3 sensor/router_sensor.py --dry-run "/menu"

ok "Rebuild complete."
