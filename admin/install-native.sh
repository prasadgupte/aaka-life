#!/usr/bin/env bash
# install-native.sh — run the aaka sensor natively (systemd + cron), no Docker.
#
# The sensor is a Telegram long-poller (OpenClaw-free) — it needs no container.
# This installs a venv, the systemd unit, AND the periodic-jobs crontab that the
# old container entrypoint set up (summaries, outbox flush, gmail poll, etc.).
# Idempotent; safe to re-run after a `git pull` on the VPS.
#
# Run on the VPS as root, from the repo root:
#   sudo bash admin/install-native.sh
#
# Rollback to Docker:
#   systemctl disable --now aaka-sensor ; crontab -r
#   docker start aaka-sensor   # (rollback image also tagged aaka-sensor:rollback)
set -euo pipefail

REPO="${AAKA_BASE:-/opt/aaka-repo}"
CONFIG="${AAKA_CONFIG_DIR:-/opt/aaka-config}"
PYBIN="$REPO/venv/bin/python"
cd "$REPO"
mkdir -p "$CONFIG/logs"

echo "==> 1. Python venv + sensor deps"
command -v python3 >/dev/null || { echo "python3 missing"; exit 1; }
if ! python3 -c "import ensurepip" 2>/dev/null; then
  PYVER="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  apt-get update -qq && apt-get install -y -qq "python${PYVER}-venv"
fi
# Never rsync a venv from another machine — rebuild with the host python.
if [ ! -x venv/bin/python ]; then rm -rf venv && python3 -m venv venv; fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements_sensor.txt
venv/bin/python -c "import requests, yaml, google.oauth2, googleapiclient" && echo "    deps OK"

echo "==> 2. Token perms (match the container entrypoint)"
[ -d "$CONFIG/tokens" ] && find "$CONFIG/tokens" -type f -name '*.json' -exec chmod 600 {} + || true

echo "==> 3. systemd unit (the long-poll sensor)"
install -m 0644 deploy/aaka-sensor.service /etc/systemd/system/aaka-sensor.service
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/aaka-sensor.service

echo "==> 4. Periodic jobs (crontab) — cron doesn't inherit service env, so source a file"
cat > /etc/aaka-cron-env <<ENV
set -a
[ -f $REPO/.env ] && . $REPO/.env
AAKA_CONFIG_DIR=$CONFIG
AAKA_BASE=$REPO
AAKA_ROLE=sensor
AAKA_CONTEXT=family
LLM_PROVIDER=gemini
QUEUE_DB=$CONFIG/data/queue/butler.db
set +a
ENV
L="$CONFIG/logs"
cat > "$CONFIG/aaka.crontab" <<CRON
* * * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN sensor/flush_outbox.py >> $L/outbox_flush.log 2>&1
* * * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN sensor/scheduled_sender.py >> $L/scheduled_sender.log 2>&1
0 * * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN sensor/scheduled_summaries.py --check >> $L/summaries.log 2>&1
*/30 * * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN skills/calendar/sidecar_sync.py >> $L/sidecar_sync.log 2>&1
0 23 * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN sensor/check_token_expiry.py >> $L/token_expiry.log 2>&1
*/10 * * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN sensor/gmail_poller.py >> $L/gmail_poller.log 2>&1
30 7 * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN sensor/error_digest.py >> $L/error_digest.log 2>&1
*/5 * * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN -c "from aaka_queue.queue import expire_reply_requests; n=expire_reply_requests(); n and print(f'expired {n}')" >> $L/reply_sweep.log 2>&1
* * * * * . /etc/aaka-cron-env && cd $REPO && $PYBIN sensor/tool_runner.py --due >> $L/tool_runner.log 2>&1
CRON
crontab "$CONFIG/aaka.crontab"
systemctl enable --now cron 2>/dev/null || systemctl enable --now crond 2>/dev/null || true
echo "    crontab installed ($(crontab -l | grep -c .) jobs); cron: $(systemctl is-active cron 2>/dev/null || systemctl is-active crond)"

echo "==> 5. Cut over from Docker (only one poller may hold the Telegram getUpdates lock)"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx aaka-sensor; then
  docker update --restart=no aaka-sensor >/dev/null || true
  docker stop aaka-sensor >/dev/null
  echo "    stopped Docker container (preserved for rollback)"
fi

echo "==> 6. Start the native sensor"
systemctl enable --now aaka-sensor
sleep 6
systemctl is-active aaka-sensor >/dev/null \
  && echo "    aaka-sensor: active ✅" \
  || { echo "    aaka-sensor FAILED — journalctl -u aaka-sensor"; exit 1; }
echo "Done. Logs: journalctl -u aaka-sensor -f  ·  crons: $CONFIG/logs/*.log"
