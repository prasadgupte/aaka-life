#!/usr/bin/env bash
# wa-sidecar/start.sh — convenience launcher for the Baileys WhatsApp sidecar.
#
# Dev / test use only. Runs the Node sidecar in the foreground so the QR code
# prints to the terminal. Pair with a THROWAWAY / TEST WhatsApp number only —
# pairing the live/prod number evicts the existing session (error 440).
#
#   cd /Users/Shared/aaka-repo/wa-sidecar && npm install   # one-time
#   ./start.sh
#
# Env (all optional; sensible defaults):
#   AAKA_CONFIG_DIR  auth state parent (default /Users/Shared/aaka-repo-config)
#   WA_AUTH_DIR      auth state dir    (default $AAKA_CONFIG_DIR/whatsapp-auth)
#   WA_SIDECAR_PORT  HTTP API port     (default 18792)
#   WA_RECEIVER_URL  inbound forward   (default http://127.0.0.1:18793/inbound)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AAKA_CONFIG_DIR="${AAKA_CONFIG_DIR:-/Users/Shared/aaka-repo-config}"
export WA_AUTH_DIR="${WA_AUTH_DIR:-$AAKA_CONFIG_DIR/whatsapp-auth}"
export WA_SIDECAR_PORT="${WA_SIDECAR_PORT:-18792}"
export WA_RECEIVER_URL="${WA_RECEIVER_URL:-http://127.0.0.1:18793/inbound}"

if [ ! -d "$HERE/node_modules" ]; then
  echo "wa-sidecar: node_modules missing — run 'npm install' in $HERE first." >&2
  exit 1
fi

echo "wa-sidecar: auth dir = $WA_AUTH_DIR"
echo "wa-sidecar: port     = $WA_SIDECAR_PORT"
exec node "$HERE/index.js"
