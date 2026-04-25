#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

DB="${AAKA_CONFIG_DIR:-${AAKA_CONFIG_DIR:-$HOME/.aaka}}/data/queue/butler.db"
LOG="${AAKA_CONFIG_DIR:-${AAKA_CONFIG_DIR:-$HOME/.aaka}}/logs/queueworker.log"

echo -e "${BOLD}Queue status${NC}"
if [ ! -f "$DB" ]; then warn "No DB at $DB"; exit 1; fi

echo ""
info "Status counts:"
sqlite3 "$DB" \
    "SELECT status, COUNT(*) FROM queue_items GROUP BY status ORDER BY status;" \
    | awk -F'|' '{printf "  %-22s %s\n", $1, $2}'

echo ""
info "Last 10 items (newest first):"
sqlite3 "$DB" \
    "SELECT substr(id,1,8), intent, status, updated_at, substr(coalesce(result,''),1,60) \
     FROM queue_items ORDER BY updated_at DESC LIMIT 10;" \
    | column -t -s '|'

echo ""
if [ -f "$LOG" ]; then
    info "Last 5 executor log lines (queueworker.log):"
    tail -5 "$LOG" | sed 's/^/  /'
else
    warn "No queueworker.log yet at $LOG"
fi
