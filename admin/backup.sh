#!/usr/bin/env bash
# admin/backup.sh — Backup VPS config to local aaka-repo-config
#
# Rsyncs /opt/aaka-config/ from VPS to $LOCAL_CONFIG_DIR (default: /Users/Shared/aaka-repo-config).
# Run from Mac: bash admin/backup.sh
#
# What's backed up:
#   config/           aaka.yaml, message_send.json
#   tokens/           Google OAuth credentials + token
#   .env              All secrets/env vars
# Note: the WhatsApp session now lives on the Mac (wa-sidecar's whatsapp-auth/),
# not the VPS — it's already local, so it's not part of this VPS→Mac backup.
#
# What's excluded:
#   data/queue/       SQLite DB (ephemeral; recreated from schema on restore)
#   data/calendar/    Generated files; recreated by sidecar_sync
#   logs/             Not worth keeping
#
# Restore on a new VPS: bash admin/deploy.sh  (step 4 detects .env in config dir)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/detect.sh"

VPS_HOST="${VPS_HOST:-${AAKA_VPS_HOST:-aaka-away}}"
VPS_CONFIG="/opt/aaka-config"
LOCAL_CONFIG="${LOCAL_CONFIG_DIR:-${AAKA_CONFIG_DIR:-$HOME/.aaka}}"

if [ "$INSTANCE" != "local" ]; then
    echo "ERROR: Run backup.sh from your Mac, not the VPS." >&2
    exit 1
fi

header "Aaka Backup — VPS → Local"
info "From: ${VPS_HOST}:${VPS_CONFIG}/"
info "To:   ${LOCAL_CONFIG}/"
echo ""

mkdir -p "$LOCAL_CONFIG"

rsync -avz --progress \
    --exclude='data/queue/' \
    --exclude='data/calendar/' \
    --exclude='logs/' \
    --exclude='*.log' \
    "${VPS_HOST}:${VPS_CONFIG}/" \
    "${LOCAL_CONFIG}/"

echo ""
ok "Backup complete → ${LOCAL_CONFIG}/"
info "To restore on a new VPS: bash admin/deploy.sh"
info "  deploy.sh detects ${LOCAL_CONFIG}/.env automatically."
