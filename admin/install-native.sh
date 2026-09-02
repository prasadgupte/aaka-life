#!/usr/bin/env bash
# install-native.sh — run the aaka sensor natively (systemd), no Docker.
#
# The sensor is a Telegram long-poller (OpenClaw-free) — it needs no container.
# This installs a venv + systemd units and cuts over from the Docker container
# if one is running. Idempotent; safe to re-run after a `git pull` on the VPS.
#
# Run on the VPS as root, from the repo root:
#   sudo bash admin/install-native.sh
#
# Rollback to Docker:
#   systemctl disable --now aaka-sensor aaka-tool-runner.timer
#   docker start aaka-sensor   # (rollback image also tagged aaka-sensor:rollback)
set -euo pipefail

REPO="${AAKA_BASE:-/opt/aaka-repo}"
CONFIG="${AAKA_CONFIG_DIR:-/opt/aaka-config}"
cd "$REPO"

echo "==> 1. Python venv + sensor deps"
command -v python3 >/dev/null || { echo "python3 missing"; exit 1; }
# Debian/Ubuntu ship venv separately; install if ensurepip is unavailable.
if ! python3 -c "import ensurepip" 2>/dev/null; then
  PYVER="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  apt-get update -qq && apt-get install -y -qq "python${PYVER}-venv"
fi
# The venv must be built by the host python — never rsync one from another machine.
if [ ! -x venv/bin/python ]; then
  rm -rf venv && python3 -m venv venv
fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements_sensor.txt
venv/bin/python -c "import requests, yaml, google.oauth2, googleapiclient" \
  && echo "    deps OK"

echo "==> 2. Token perms (match the container entrypoint)"
[ -d "$CONFIG/tokens" ] && find "$CONFIG/tokens" -type f -name '*.json' -exec chmod 600 {} + || true

echo "==> 3. Install systemd units"
install -m 0644 deploy/aaka-sensor.service       /etc/systemd/system/aaka-sensor.service
install -m 0644 deploy/aaka-tool-runner.service  /etc/systemd/system/aaka-tool-runner.service
install -m 0644 deploy/aaka-tool-runner.timer    /etc/systemd/system/aaka-tool-runner.timer
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/aaka-sensor.service

echo "==> 4. Cut over from Docker (only one poller may hold the Telegram getUpdates lock)"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx aaka-sensor; then
  docker update --restart=no aaka-sensor >/dev/null || true
  docker stop aaka-sensor >/dev/null
  echo "    stopped Docker container (preserved for rollback)"
fi

echo "==> 5. Start native services"
systemctl enable --now aaka-sensor
systemctl enable --now aaka-tool-runner.timer
sleep 6
systemctl is-active aaka-sensor >/dev/null \
  && echo "    aaka-sensor: active ✅" \
  || { echo "    aaka-sensor FAILED — journalctl -u aaka-sensor"; exit 1; }
echo "Done. Logs: journalctl -u aaka-sensor -f"
