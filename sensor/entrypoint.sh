#!/usr/bin/env bash
set -euo pipefail

# Channel set — default Telegram-only (OpenClaw-free). WhatsApp (OpenClaw/Baileys)
# is enabled only when "whatsapp" is in ENABLED_CHANNELS.
ENABLED_CHANNELS="${ENABLED_CHANNELS:-telegram}"
case ",${ENABLED_CHANNELS}," in
    *,whatsapp,*) WHATSAPP_ON=1 ;;
    *)            WHATSAPP_ON=0 ;;
esac
echo "[entrypoint] ENABLED_CHANNELS=${ENABLED_CHANNELS} (whatsapp=${WHATSAPP_ON})"

# ── OpenClaw setup (WhatsApp only) ────────────────────────────────────────────
if [ "$WHATSAPP_ON" = "1" ]; then

# Ensure openclaw config dir exists (may be a mounted volume)
mkdir -p /home/aaka/.openclaw

# Always re-template agent.yaml (cheap, keeps it in sync with env vars)
envsubst < /app/gateway/openclaw/agent.yaml.example > /home/aaka/.openclaw/agent.yaml

# Strip bindings with empty IDs (env var not set) so openclaw.json groupPolicy takes over
python3 - <<'PYEOF'
import yaml, re
path = '/home/aaka/.openclaw/agent.yaml'
with open(path) as f:
    cfg = yaml.safe_load(f)
original = len(cfg.get('bindings', []))
cfg['bindings'] = [b for b in cfg.get('bindings', []) if b.get('id', '').strip()]
removed = original - len(cfg['bindings'])
if removed:
    with open(path, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    print(f'[entrypoint] Removed {removed} agent.yaml binding(s) with empty id (env var not set)')
PYEOF

# Inject gateway config with controlUi fallback for non-loopback binding
python3 -c "
import json, os
cfg_path = '/home/aaka/.openclaw/openclaw.json'
cfg = {}
if os.path.exists(cfg_path):
    with open(cfg_path) as f: cfg = json.load(f)
cfg.setdefault('gateway', {}).setdefault('controlUi', {})['dangerouslyAllowHostHeaderOriginFallback'] = True
# Register router as a CLI backend so model 'router/router' resolves to router_sensor.py
cfg.setdefault('agents', {}).setdefault('defaults', {}).setdefault('cliBackends', {})['router'] = {
    'command': '/usr/local/bin/python3',
    'args': ['/app/sensor/router_sensor.py'],
    'input': 'arg',
    'output': 'text',
    'sessionMode': 'none'
}
# Disable openclaw's Telegram connector — telegram_poller.py handles inbound polling
# directly and is more reliable (openclaw's Telegram connector silently stops polling).
tg = cfg.setdefault('channels', {}).setdefault('telegram', {})
tg['enabled'] = False
tg['groupPolicy'] = 'open'
# Remove invalid streaming sub-key (openclaw >=2026.4 rejects streaming.mode)
tg.pop('streaming', None)
# WhatsApp: only enable when WHATSAPP_PHONE is set (prevents dev containers from
# stealing the VPS WhatsApp Web session and causing 440 session conflicts)
if os.environ.get('WHATSAPP_PHONE', '').strip():
    cfg.setdefault('channels', {})['whatsapp'] = {'enabled': True, 'groupPolicy': 'open', 'dmPolicy': 'allowlist'}
else:
    cfg.setdefault('channels', {})['whatsapp'] = {'enabled': False}
# Group-chat trigger patterns: allow slash commands and single-letter aliases
# without requiring an @mention of the bot
cfg.setdefault('messages', {})['groupChat'] = {
    'mentionPatterns': [
        '^/',                             # any slash command
        '^[bcdnqstw]\\\\s',                # single-letter alias + space
        '^[bcdnqstw]$',                   # bare single-letter alias
        '^#',                             # hashtag shortcuts: #done #clear #all #share
        '^[yYnN]$',                       # single-char confirm/cancel: y/n/Y/N
        '^(yes|no|cancel|force)$',        # word confirmations
        '^\\\\d[\\\\d\\\\s]*$',               # bare numbers for list check-off
        '\\\\b(today|week|status|menu|plan|block|done|buy|note|drop)\\\\b',
    ],
}
cfg['messages']['ackReactionScope'] = 'group-all'
# Remove legacy keys that openclaw >=2026.3 rejects
cfg.pop('providers', None)
if 'defaults' in cfg.get('models', {}):
    del cfg['models']['defaults']
with open(cfg_path, 'w') as f: json.dump(cfg, f, indent=2)
"

# Configure Gemini model via CLI (GEMINI_API_KEY env var provides auth automatically)
openclaw models set google/gemini-2.5-flash 2>/dev/null || true

# openclaw models set writes agents.defaults.model.primary = gemini, which overrides
# the per-agent model: router/router from agent.yaml. Replace it with router/router
# so all agents route through router_sensor.py by default (no LLM fallback).
python3 -c "
import json
cfg_path = '/home/aaka/.openclaw/openclaw.json'
with open(cfg_path) as f: cfg = json.load(f)
defaults = cfg.get('agents', {}).get('defaults', {})
defaults['model'] = {'primary': 'router/router', 'fallbacks': []}
with open(cfg_path, 'w') as f: json.dump(cfg, f, indent=2)
"

fi  # end WhatsApp/OpenClaw setup

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

# Ensure openclaw dir is owned by aaka (harmless if unused)
[ -d /home/aaka/.openclaw ] && chown -R aaka:aaka /home/aaka/.openclaw 2>/dev/null || true

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
export GATEWAY_BACKEND=${GATEWAY_BACKEND:-openclaw}
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
# Telegram runs via the multi-bot supervisor (one native poller per bot).
if [ "$WHATSAPP_ON" = "1" ]; then
    # WhatsApp needs the OpenClaw daemon in the foreground; run Telegram alongside it.
    (while true; do
        gosu aaka env ENABLED_CHANNELS="$ENABLED_CHANNELS" python3 -u /app/sensor/telegram_multibot.py \
            >> /config/logs/telegram_poller.log 2>&1
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [entrypoint] telegram supervisor exited (code $?), restarting in 5s" \
            >> /config/logs/telegram_poller.log
        sleep 5
    done) &
    # Cap Node heap to 400 MB — openclaw loads large plugin deps (playwright, pdfjs, etc.).
    exec gosu aaka env NODE_OPTIONS="--max-old-space-size=400" openclaw gateway run --bind lan --allow-unconfigured
else
    # Telegram-only (default): the supervisor is the foreground process. No OpenClaw.
    exec gosu aaka env ENABLED_CHANNELS="$ENABLED_CHANNELS" python3 -u /app/sensor/telegram_multibot.py
fi
