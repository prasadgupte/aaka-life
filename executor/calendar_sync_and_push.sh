#!/usr/bin/env bash
# executor/calendar_sync_and_push.sh — Calendar sync + VPS push wrapper
# Called by launchd (com.aaka.calendarsync) instead of sidecar_sync.py directly.
#
# Step 1: Run Python calendar sync (hard failure propagates to launchd)
# Step 2: rsync calendar files to VPS (soft — VPS unreachable is a warning, not an error)
set -euo pipefail

AAKA_BASE="${AAKA_BASE:-/Users/Shared/aaka-repo}"
AAKA_CONFIG_DIR="${AAKA_CONFIG_DIR:-/Users/Shared/aaka-repo-config}"
PYTHON="$AAKA_BASE/venv/bin/python3"
CAL_DIR="$AAKA_CONFIG_DIR/data/calendar"
VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"
VPS_CAL_DIR="/opt/aaka-config/data/calendar/"

# Step 1: Python calendar sync (must succeed)
"$PYTHON" "$AAKA_BASE/skills/calendar/sidecar_sync.py"

# Step 2: Push calendar files to VPS (soft-optional)
if ssh -o BatchMode=yes -o ConnectTimeout=5 "$VPS_HOST" echo ok &>/dev/null; then
    rsync -az \
        "$CAL_DIR/today.md" \
        "$CAL_DIR/weekly.md" \
        "$CAL_DIR/weekly_events.json" \
        "$CAL_DIR/member_events.json" \
        "$CAL_DIR"/today_*.md \
        "$CAL_DIR"/weekly_*.md \
        "${VPS_HOST}:${VPS_CAL_DIR}"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] INFO  VPS push OK"
else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARN  ${VPS_HOST} unreachable — skipping push"
fi
