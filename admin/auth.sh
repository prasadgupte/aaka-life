#!/usr/bin/env bash
# auth.sh — token validity, active senders, security report
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/detect.sh"

PUSH_TOKENS=false
for arg in "$@"; do [[ "$arg" == "--push-tokens" ]] && PUSH_TOKENS=true; done

PYTHON="${REPO_DIR}/venv/bin/python3"
QUEUE_DB="${AAKA_CONFIG_DIR}/data/queue/butler.db"

echo -e "${BOLD}Auth & Sender Report${NC}"
echo "===================="

# 1. Token validation (local only)
header "1. Token Validity"
if [ "$INSTANCE" = "local" ]; then
    result=$( AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$AAKA_CONFIG_DIR" \
        "$PYTHON" "$REPO_DIR/skills/auth_check.py" --heartbeat 2>/dev/null; true)
    # Handle both single object and array
    echo "$result" | "$PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
if isinstance(data, dict): data = [data]
for r in data:
    mid = r.get('member', 'unknown')
    if r.get('valid'):
        exp = r.get('expires_at','?')
        lat = r.get('latency_ms')
        lat_str = f' GCal={lat}ms' if lat else ''
        print(f'  \033[32m✓\033[0m {mid}  token={r[\"token_file\"]}  expires={exp}{lat_str}')
    else:
        print(f'  \033[31m✗\033[0m {mid}  ERROR: {r.get(\"error\",\"unknown\")}')
        print(f'    Hint: re-run OAuth flow and place token in \$AAKA_CONFIG_DIR/tokens/')
"
else
    warn "Token validation only available on local instance"
fi

# 2. Active senders
header "2. Active Senders"
if [[ -f "$QUEUE_DB" ]]; then
    "$PYTHON" -c "
import sys, json
sys.path.insert(0, '$REPO_DIR')
import aaka_config, sqlite3
conn = sqlite3.connect('$QUEUE_DB')
rows = conn.execute(\"SELECT sender, COUNT(*) as cnt FROM queue_items GROUP BY sender ORDER BY cnt DESC\").fetchall()
conn.close()
cfg = aaka_config.load()
members = cfg.get('members', [])
known_ids = set()
for m in members:
    for field in ['whatsapp','telegram','email','phone']:
        v = m.get(field)
        if v: known_ids.add(str(v))

for sender, cnt in rows:
    label = 'known member' if str(sender) in known_ids else '\033[33mUNKNOWN SENDER\033[0m'
    print(f'  {sender}  ({cnt} messages)  [{label}]')
if not rows:
    print('  (no queue activity)')
" 2>/dev/null || warn "Could not query queue DB"
else
    warn "Queue DB not found: $QUEUE_DB"
fi

# 3. Security report
header "3. Security Report"
if [[ -f "$QUEUE_DB" ]]; then
    "$PYTHON" -c "
import sys, json
sys.path.insert(0, '$REPO_DIR')
import aaka_config, sqlite3
conn = sqlite3.connect('$QUEUE_DB')
rows = conn.execute(\"SELECT sender, COUNT(*) as cnt FROM queue_items GROUP BY sender\").fetchall()
cfg = aaka_config.load()
members = cfg.get('members', [])
known_ids = set()
for m in members:
    for field in ['whatsapp','telegram','email','phone']:
        v = m.get(field)
        if v: known_ids.add(str(v))
unknown = [(s,c) for s,c in rows if str(s) not in known_ids]
if not unknown:
    print('  \033[32mNo activity from unknown senders\033[0m')
else:
    for sender, cnt in unknown:
        details = conn.execute(\"SELECT intent, status, created_at FROM queue_items WHERE sender=? ORDER BY created_at DESC LIMIT 5\", (sender,)).fetchall()
        print(f'  \033[31mUNKNOWN:\033[0m {sender} ({cnt} messages)')
        for intent, status, ts in details:
            print(f'    intent={intent} status={status} at={ts}')
conn.close()
" 2>/dev/null || warn "Could not query queue DB"
fi

# 4. Token push to VPS (--push-tokens only, local only)
if $PUSH_TOKENS; then
    header "4. Token Push to VPS"
    if [ "$INSTANCE" != "local" ]; then
        warn "Token push only available on local instance"
    else
        SSH_KEY="$HOME/.ssh/aaka_executor"
        VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"
        if ! ssh -i "$SSH_KEY" -o ConnectTimeout=5 -o BatchMode=yes "$VPS_HOST" echo ok &>/dev/null; then
            warn "Cannot reach $VPS_HOST via SSH key $SSH_KEY — skipping token push"
        else
            ok "VPS reachable"
            "$PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_DIR')
import aaka_config
for m in aaka_config.auth_members():
    info = aaka_config.auth_for(m['id'])
    if info:
        print(info['token_file'])
" 2>/dev/null | while read -r tf; do
                [[ -f "$tf" ]] || continue
                fname="$(basename "$tf")"
                if scp -i "$SSH_KEY" "$tf" "$VPS_HOST:/opt/aaka-config/tokens/$fname" 2>/dev/null; then
                    ok "pushed $fname"
                else
                    warn "failed to push $fname"
                fi
            done
        fi
    fi
fi

# 5. Reauth instructions
header "5. Reauth Instructions"
echo "  To re-authorize a Google account:"
echo "    bash admin/aaka.sh reauth            # interactive menu"
echo "    bash admin/aaka.sh reauth aakash     # direct"
echo ""
echo "  credentials.json must be at: ${AAKA_CONFIG_DIR}/config/credentials.json"
