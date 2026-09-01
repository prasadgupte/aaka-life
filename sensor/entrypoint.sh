#!/usr/bin/env bash
set -euo pipefail

# Sensor is OpenClaw-free. It receives Telegram natively via telegram_multibot.py.
# WhatsApp is handled entirely on the Mac by the wa-sidecar (Baileys) — the VPS
# container never touches WhatsApp, so no OpenClaw daemon is needed here.
ENABLED_CHANNELS="${ENABLED_CHANNELS:-telegram}"
echo "[entrypoint] ENABLED_CHANNELS=${ENABLED_CHANNELS} (telegram native; whatsapp via Mac wa-sidecar)"

# Write sensor version info for /status code
mkdir -p /config/data
{
  echo "deployed=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  git -C /app log -1 --format='commit=%h %s' 2>/dev/null || echo "commit=unknown"
} > /config/data/.sensor_version

# Harden token file permissions (runs as root before gosu drop)
if [ -d /config/tokens ]; then
    find /config/tokens -type f -name "*.json" -exec chmod 600 {} \;
    chown -R aaka:aaka /config/tokens 2>/dev/null || true
fi

# Ensure data subdirs exist (created lazily on first use, but better to have them ready)
mkdir -p /config/data/tasks /config/data/notes /config/data/telegram_media /config/data/staging

# Give aaka write access to dirs router_sensor.py writes at runtime
# (queue DB, logs, staging, lists, notes, tasks — /config is host-mounted as root)
for _dir in /config/data /config/logs; do
    [ -d "$_dir" ] && chown -R aaka:aaka "$_dir" 2>/dev/null || true
done
# Fallback: chmod a+w on the queue DB and related WAL files in case chown failed.
# NOTE: In rootless Docker, container root ≠ host root, so chown above may silently
# fail for files owned by a host UID outside the container's subuid range.
# If butler.db remains unwritable after deploy, fix once from the VPS host:
#   sudo chmod a+w /opt/aaka-config/data/queue/butler.db
# SQLite's own locking protects concurrent access; world-writable is safe here.
for _f in /config/data/queue/butler.db /config/data/queue/butler.db-shm /config/data/queue/butler.db-wal; do
    [ -f "$_f" ] && chmod a+w "$_f" 2>/dev/null || true
done
# Also ensure the queue directory itself is writable (SQLite needs to create lock files)
[ -d /config/data/queue ] && chmod a+w /config/data/queue 2>/dev/null || true
# Staging and telegram_media dirs need to be writable by aaka (router_sensor.py creates subdirs)
for _d in /config/data/staging /config/data/telegram_media; do
    [ -d "$_d" ] && chmod a+w "$_d" 2>/dev/null || true
done

# Ensure contacts write dirs exist with correct ownership (bday_lists created by cron as root otherwise)
mkdir -p /config/data/contacts/bday_lists
chown -R aaka:aaka /config/data/contacts 2>/dev/null || true

# Install + start cron jobs:
#   outbox flush       — every 3 min
#   scheduled summaries — hourly
#   calendar sync       — every 30 min (writes today.md / weekly.md on VPS)
#   token expiry check  — nightly at 23:00
# Write env file for cron jobs (cron doesn't inherit container env)
cat > /etc/aaka-cron-env << ENVEOF
export AAKA_CONFIG_DIR=/config
export AAKA_BASE=/app
export AAKA_ROLE=sensor
export AAKA_CONTEXT=${AAKA_CONTEXT:-family}
export LLM_PROVIDER=${LLM_PROVIDER:-gemini}
export ENABLED_CHANNELS=${ENABLED_CHANNELS}
export QUEUE_DB=${QUEUE_DB:-/config/data/queue/butler.db}
export GEMINI_API_KEY=${GEMINI_API_KEY:-}
export ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
export TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
export TELEGRAM_GROUP_ID=${TELEGRAM_GROUP_ID:-}
export TELEGRAM_USER_ID=${TELEGRAM_USER_ID:-}
export WHATSAPP_GROUP_JID=${WHATSAPP_GROUP_JID:-}
export WHATSAPP_PHONE=${WHATSAPP_PHONE:-}
ENVEOF

{
echo "* * * * * . /etc/aaka-cron-env && cd /app && /usr/local/bin/python3 sensor/flush_outbox.py >> /config/logs/outbox_flush.log 2>&1"
echo "* * * * * . /etc/aaka-cron-env && cd /app && /usr/local/bin/python3 sensor/scheduled_sender.py >> /config/logs/scheduled_sender.log 2>&1"
echo "0 * * * * . /etc/aaka-cron-env && cd /app && /usr/local/bin/python3 sensor/scheduled_summaries.py --check >> /config/logs/summaries.log 2>&1"
echo "*/30 * * * * . /etc/aaka-cron-env && cd /app && /usr/local/bin/python3 skills/calendar/sidecar_sync.py >> /config/logs/sidecar_sync.log 2>&1"
echo "0 23 * * * . /etc/aaka-cron-env && cd /app && /usr/local/bin/python3 sensor/check_token_expiry.py >> /config/logs/token_expiry.log 2>&1"
echo "*/10 * * * * . /etc/aaka-cron-env && cd /app && /usr/local/bin/python3 sensor/gmail_poller.py >> /config/logs/gmail_poller.log 2>&1"
echo "30 7 * * * . /etc/aaka-cron-env && cd /app && /usr/local/bin/python3 sensor/error_digest.py >> /config/logs/error_digest.log 2>&1"
echo "*/5 * * * * . /etc/aaka-cron-env && cd /app && /usr/local/bin/python3 -c 'from aaka_queue.queue import expire_reply_requests; n = expire_reply_requests(); n and print(f\"expired {n} stale reply_request(s)\")' >> /config/logs/reply_sweep.log 2>&1"
} | crontab -
service cron start

# Start sensor HTTP server in background for health checks
python3 sensor/router_sensor.py --serve --port 18790 &

# ── Foreground process ────────────────────────────────────────────────────────
# Telegram multi-bot supervisor (one native poller per bot) is the foreground
# process. No OpenClaw. WhatsApp, if used, runs on the Mac via the wa-sidecar.
exec gosu aaka env ENABLED_CHANNELS="$ENABLED_CHANNELS" python3 -u /app/sensor/telegram_multibot.py
