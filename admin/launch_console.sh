#!/usr/bin/env bash
# admin/launch_console.sh — start the Aaka Console on port 8003 (localhost).
#
# The console is one FastAPI app serving three tabs (Console · Tasks · Status):
#   • Console — real web channel (POST /webui/messages + SSE /webui/stream)
#   • Tasks   — taskboard router mounted at /tasks, reskinned to brand tokens
#   • Status  — admin/setup_check.py --json capability board + log tail
#
# Brand CSS is vendored into executor/webui/brand/. If aaka-site/brand/ changes,
# re-run with --sync-brand to refresh the vendored copies.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="${AAKA_CONFIG_DIR:-/Users/Shared/aaka-repo-config}"
PORT="${AAKA_CONSOLE_PORT:-8003}"

# Optional brand re-vendor step.
if [ "${1:-}" = "--sync-brand" ]; then
  BRAND_SRC="/Users/Shared/aaka-site/brand"
  BRAND_DEST="$REPO_DIR/executor/webui/brand"
  if [ -d "$BRAND_SRC" ]; then
    cp "$BRAND_SRC"/tokens.css "$BRAND_SRC"/components.css "$BRAND_SRC"/global.css "$BRAND_DEST/"
    echo "Brand CSS synced from $BRAND_SRC"
  else
    echo "WARN: $BRAND_SRC not found — skipping brand sync" >&2
  fi
  # Re-vendor the shared markdown renderer into the console static dir.
  cp "$REPO_DIR/executor/webui/static/md.js" "$REPO_DIR/executor/console/static/md.js" \
    && echo "md.js synced from executor/webui/static"
  shift || true
fi

# Port check — don't clobber whatever is already bound.
if lsof -i ":$PORT" -sTCP:LISTEN -t &>/dev/null; then
  echo "ERROR: port $PORT already in use" >&2
  exit 1
fi

if [ ! -x "$REPO_DIR/venv/bin/python3" ]; then
  echo "ERROR: venv python not found at $REPO_DIR/venv/bin/python3" >&2
  exit 1
fi

echo "Aaka Console starting on http://localhost:$PORT (config=$CONFIG_DIR)"
cd "$REPO_DIR"

# Open the browser once the server is up. Backgrounded so it fires after the
# exec below has replaced this shell with uvicorn (which never returns).
if [ "${AAKA_CONSOLE_NO_OPEN:-}" != "1" ] && command -v open &>/dev/null; then
  ( sleep 2; open "http://localhost:$PORT" ) &
fi

exec "$REPO_DIR/venv/bin/python3" executor/console/server.py \
  --config "$CONFIG_DIR" \
  --host 127.0.0.1 \
  --port "$PORT"
