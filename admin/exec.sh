#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/detect.sh"

VPS_HOST="${VPS_HOST:-${AAKA_VPS_HOST:-aaka-away}}"
VPS_DB_PATH="${VPS_DB_PATH:-/opt/aaka-config/data/queue/butler.db}"
LOCAL_DB_PATH="${LOCAL_DB_PATH:-${AAKA_CONFIG_DIR}/data/queue/butler.db}"
AAKA_BASE="${AAKA_BASE:-$REPO_DIR}"
LOG="${AAKA_CONFIG_DIR}/logs/sync.log"

_queue_counts() {
    if [ -f "$LOCAL_DB_PATH" ]; then
        sqlite3 "$LOCAL_DB_PATH" \
          "SELECT status||'='||COUNT(*) FROM queue_items GROUP BY status;" \
          2>/dev/null | tr '\n' '  ' || echo "(db unreadable)"
    else
        echo "(no local DB)"
    fi
}

CMD="${1:-help}"

case "$CMD" in
  poll-sensor)
    if [ "$INSTANCE" = "vps" ]; then
        fail "poll-sensor must be run on local Mac (executor side)"
        exit 1
    fi
    echo -e "${BOLD}&Home: polling &Away (VPS)${NC}"
    info "VPS host : $VPS_HOST"
    info "Queue before: $(_queue_counts)"
    echo ""
    # Run one sync cycle via the Python module directly
    VPS_HOST="$VPS_HOST" VPS_DB_PATH="$VPS_DB_PATH" \
    AAKA_CONFIG_DIR="$AAKA_CONFIG_DIR" QUEUE_DB="$LOCAL_DB_PATH" \
      "$REPO_DIR/venv/bin/python3" -c "
from executor.vps_sync import load_config, run_sync_cycle
cfg = load_config()
if cfg:
    s = run_sync_cycle(cfg)
    print(f'[sync] pulled={s.pulled} pushed={s.pushed} outbox={s.outbox} ({s.duration_ms}ms)')
else:
    print('VPS sync not configured (set VPS_HOST + VPS_DB_PATH)')
"
    echo ""
    info "Queue after:  $(_queue_counts)"
    ok "Done."
    ;;

  help|--help|-h|"")
    echo -e "${BOLD}Aaka &Home Commands${NC}"
    echo "Usage: bash admin/exec.sh <command>"
    echo ""
    printf "  %-18s %s\n" "poll-sensor" "Trigger immediate sync+process cycle (rsync VPS → Mac → run → push)"
    echo ""
    echo "Run 'bash admin/aaka.sh diagnose' for full health check including last poll time."
    ;;

  *)
    echo "Unknown command: $CMD"
    echo "Run 'bash admin/exec.sh help' for usage."
    exit 1
    ;;
esac
