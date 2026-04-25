#!/usr/bin/env bash
# admin/aaka.sh — Aaka admin entry point
#
# Usage:
#   bash admin/aaka.sh diagnose
#   bash admin/aaka.sh deploy
#   bash admin/aaka.sh test

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CMD="${1:-help}"

case "$CMD" in
    diagnose)
        bash "$SCRIPT_DIR/diagnose.sh"
        ;;
    setup)
        bash "$SCRIPT_DIR/setup.sh" "${@:2}"
        ;;
    deploy)
        bash "$SCRIPT_DIR/deploy.sh"
        ;;
    test)
        bash "$SCRIPT_DIR/test.sh"
        ;;
    rebuild)
        bash "$SCRIPT_DIR/rebuild.sh"
        ;;
    exec)
        bash "$SCRIPT_DIR/exec.sh" "${@:2}"
        ;;
    skills)
        bash "$SCRIPT_DIR/skills.sh"
        ;;
    auth)
        bash "$SCRIPT_DIR/auth.sh" "${@:2}"
        ;;
    sync)
        bash "$SCRIPT_DIR/sync.sh" "${@:2}"
        ;;
    reauth)
        REPO_DIR="$(dirname "$SCRIPT_DIR")"
        PYTHON="$REPO_DIR/venv/bin/python3"
        CONFIG_DIR="${AAKA_CONFIG_DIR:-$HOME/.aaka}"

        # If a direct arg was passed (e.g. aaka.sh reauth aakash), forward it
        if [ "${2:-}" != "" ]; then
            AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$CONFIG_DIR" \
                "$PYTHON" "$SCRIPT_DIR/reauth.py" "${@:2}"
            exit 0
        fi

        # Build member list from aaka_config
        MEMBERS_JSON=$(AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$CONFIG_DIR" \
            "$PYTHON" -c "
import sys, os, json
sys.path.insert(0, os.environ['AAKA_BASE'])
import aaka_config
members = [{'id': m['id'], 'name': m.get('name', m['id']), 'email': m.get('email','')}
           for m in aaka_config.auth_members()]
print(json.dumps(members))
" 2>/dev/null || echo "[]")

        echo ""
        echo "  Re-authorize a Google OAuth token"
        echo "  ─────────────────────────────────────────────────────"
        echo ""
        echo "    (1)  VPS token — calendar + tasks  (&away)"

        # Print home members starting at index 2
        IDX=2
        while IFS= read -r line; do
            NAME=$(echo "$line" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d['name'])" 2>/dev/null)
            EMAIL=$(echo "$line" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('email',''))" 2>/dev/null)
            LABEL="$NAME"
            [ -n "$EMAIL" ] && LABEL="$NAME  <$EMAIL>"
            echo "    ($IDX)  $LABEL  (&home)"
            IDX=$((IDX + 1))
        done < <(echo "$MEMBERS_JSON" | "$PYTHON" -c "
import sys, json
for m in json.load(sys.stdin):
    print(json.dumps(m))
" 2>/dev/null)

        echo ""
        printf "  Choice: "
        read -r CHOICE

        if [ "$CHOICE" = "1" ]; then
            echo ""
            AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$CONFIG_DIR" \
                "$PYTHON" "$SCRIPT_DIR/reauth.py" --profile vps
            echo ""
            echo "  Copying token to VPS…"
            VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"
            if scp "${CONFIG_DIR}/tokens/token_vps.json" \
                   "${VPS_HOST}:/opt/aaka-config/tokens/token_vps.json" 2>&1; then
                ssh "${VPS_HOST}" "chmod 600 /opt/aaka-config/tokens/token_vps.json"
                echo "  ✓  token_vps.json deployed and secured on ${VPS_HOST}"
            else
                echo "  ✗  scp failed — copy manually:"
                echo "     scp ${CONFIG_DIR}/tokens/token_vps.json ${VPS_HOST}:/opt/aaka-config/tokens/token_vps.json"
            fi
            # Deploy aakash's token — needed by sensor/scheduled_sender.py for gmail.send
            if [ -f "${CONFIG_DIR}/tokens/token_aakash.json" ]; then
                if scp "${CONFIG_DIR}/tokens/token_aakash.json" \
                       "${VPS_HOST}:/opt/aaka-config/tokens/token_aakash.json" 2>&1; then
                    ssh "${VPS_HOST}" "chmod 600 /opt/aaka-config/tokens/token_aakash.json"
                    echo "  ✓  token_aakash.json deployed and secured on ${VPS_HOST}"
                else
                    echo "  ✗  scp failed for token_aakash.json — copy manually:"
                    echo "     scp ${CONFIG_DIR}/tokens/token_aakash.json ${VPS_HOST}:/opt/aaka-config/tokens/token_aakash.json"
                fi
            fi
            echo ""
        else
            # Resolve member id by index
            MEMBER_ID=$(echo "$MEMBERS_JSON" | "$PYTHON" -c "
import sys, json
members = json.load(sys.stdin)
idx = int('${CHOICE}') - 2
if 0 <= idx < len(members):
    print(members[idx]['id'])
" 2>/dev/null)
            if [ -z "$MEMBER_ID" ]; then
                echo "  Invalid choice."
                exit 1
            fi
            echo ""
            AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$CONFIG_DIR" \
                "$PYTHON" "$SCRIPT_DIR/reauth.py" "$MEMBER_ID"
        fi
        ;;
    vps-telegram)
        VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"
        CONTAINER="aaka-sensor"
        OC_JSON="/opt/aaka-config/openclaw-data/openclaw.json"
        echo ""
        echo "  Telegram status on ${VPS_HOST}"
        echo "  ─────────────────────────────────────────────────────"

        # Container running?
        status=$(ssh "$VPS_HOST" "docker inspect -f '{{.State.Status}}' $CONTAINER 2>/dev/null || echo 'missing'")
        echo "  Container : $status"

        # Telegram channel state from openclaw log
        echo ""
        echo "  Recent Telegram events:"
        ssh "$VPS_HOST" "docker logs $CONTAINER 2>&1 | grep -i telegram | tail -8 | sed 's/^/    /'"

        # model_fallback (candidate_failed = router_sensor.py crashing)
        echo ""
        echo "  Router errors (last 5):"
        ssh "$VPS_HOST" "docker exec $CONTAINER python3 -c \"
import json
with open('/tmp/openclaw-997/openclaw-2026-04-25.log') as f:
    lines = [l for l in f if 'candidate_failed' in l or ('ERROR' in l and 'router' in l.lower())]
for l in lines[-5:]:
    try:
        d = json.loads(l); print('   ', str(d.get('1',''))[:160])
    except: print('   ', l[:160].strip())
\" 2>/dev/null || echo '    (log not found)'"

        # /config permissions
        echo ""
        echo "  /config write permissions (need aaka ownership):"
        ssh "$VPS_HOST" "stat -c '%U %n' /opt/aaka-config/data /opt/aaka-config/logs /opt/aaka-config/data/queue 2>/dev/null | sed 's/^/    /'"

        # Offer repair
        echo ""
        printf "  Repair? Chown data+logs to aaka and restart container [y/N] "
        read -r _ans
        if [[ "$_ans" =~ ^[Yy]$ ]]; then
            echo "  Fixing permissions..."
            aaka_uid=$(ssh "$VPS_HOST" "docker exec $CONTAINER id -u aaka 2>/dev/null || echo 997")
            aaka_gid=$(ssh "$VPS_HOST" "docker exec $CONTAINER id -g aaka 2>/dev/null || echo 996")
            ssh "$VPS_HOST" "chown -R ${aaka_uid}:${aaka_gid} /opt/aaka-config/data /opt/aaka-config/logs"
            echo "  Restarting container..."
            ssh "$VPS_HOST" "docker restart $CONTAINER"
            echo "  Done. Watch logs: ssh $VPS_HOST 'docker logs -f $CONTAINER'"
        fi
        echo "  ─────────────────────────────────────────────────────"
        ;;
    vps-whatsapp)
        VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"
        CONTAINER="aaka-sensor"
        OC_DATA="/opt/aaka-config/openclaw-data"
        echo ""
        echo "  WhatsApp status on ${VPS_HOST}"
        echo "  ─────────────────────────────────────────────────────"

        # Container running?
        status=$(ssh "$VPS_HOST" "docker inspect -f '{{.State.Status}}' $CONTAINER 2>/dev/null || echo 'missing'")
        echo "  Container : $status"

        # WhatsApp channel state from docker logs
        echo ""
        echo "  Recent WhatsApp events:"
        ssh "$VPS_HOST" "docker logs $CONTAINER 2>&1 | grep -i whatsapp | tail -10 | sed 's/^/    /'"

        # Session files present?
        echo ""
        echo "  Session files in openclaw-data:"
        ssh "$VPS_HOST" "ls -lh ${OC_DATA}/ 2>/dev/null | grep -v '^total' | sed 's/^/    /'"

        # Check if session creds exist (Baileys stores them in a subdirectory)
        echo ""
        echo "  Baileys creds (session health):"
        ssh "$VPS_HOST" "find ${OC_DATA} -name 'creds.json' 2>/dev/null | sed 's/^/    /' || echo '    (none found — re-pair needed)'"

        # Offer actions
        echo ""
        echo "  Actions:"
        echo "    [1] Restart container (re-connects using existing session)"
        echo "    [2] Clear WhatsApp session + restart (forces new QR scan)"
        echo "    [3] Do nothing"
        echo ""
        printf "  Choice [1/2/3]: "
        read -r _ans

        case "$_ans" in
            1)
                echo "  Restarting container..."
                ssh "$VPS_HOST" "docker restart $CONTAINER"
                echo "  Done. Watch logs:"
                echo "    ssh $VPS_HOST 'docker logs -f $CONTAINER'"
                ;;
            2)
                echo "  Clearing WhatsApp session files..."
                ssh "$VPS_HOST" "find ${OC_DATA} -name 'creds.json' -o -name '*.json' -path '*/baileys*' 2>/dev/null | xargs rm -f"
                ssh "$VPS_HOST" "find ${OC_DATA} -maxdepth 2 -name 'session-*' -type d 2>/dev/null | xargs rm -rf"
                echo "  Restarting container..."
                ssh "$VPS_HOST" "docker restart $CONTAINER"
                echo ""
                echo "  A QR code will appear in the logs. Scan it with WhatsApp:"
                echo "    ssh $VPS_HOST 'docker logs -f $CONTAINER 2>&1 | grep -A2 QR'"
                echo "  Or watch all logs:"
                echo "    ssh $VPS_HOST 'docker logs -f $CONTAINER'"
                ;;
            *)
                echo "  No action taken."
                ;;
        esac
        echo "  ─────────────────────────────────────────────────────"
        ;;
    tunnel-claw)
        LOCAL_PORT="${2:-18789}"
        VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"
        VPS_PORT="18789"
        OC_JSON="/opt/aaka-config/openclaw-data/openclaw.json"
        TOKEN=$(ssh "$VPS_HOST" "python3 -c \"import json; d=json.load(open('${OC_JSON}')); print(d.get('gateway',{}).get('auth',{}).get('token',''))\"" 2>/dev/null || echo "")
        echo ""
        echo "  OpenClaw Dashboard"
        echo "  ─────────────────────────────────────────────────────"
        echo "  URL:   http://localhost:${LOCAL_PORT}/__openclaw__/canvas/"
        if [ -n "$TOKEN" ]; then
            echo "  Token: ${TOKEN}"
            echo ""
            echo "  Full URL with token:"
            echo "  http://localhost:${LOCAL_PORT}/__openclaw__/canvas/?token=${TOKEN}"
        else
            echo "  Token: (not found — check ${OC_JSON} on VPS)"
        fi
        echo "  ─────────────────────────────────────────────────────"
        echo "  Press Ctrl+C to close the tunnel."
        echo ""
        ssh -N -L "${LOCAL_PORT}:localhost:${VPS_PORT}" "$VPS_HOST"
        ;;
    cal)
        bash "$SCRIPT_DIR/cal.sh" "${@:2}"
        ;;
    queue)
        bash "$SCRIPT_DIR/queue.sh" "${@:2}"
        ;;
    status)
        REPO_DIR="$(dirname "$SCRIPT_DIR")"
        AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="${AAKA_CONFIG_DIR:-$HOME/.aaka}" \
            "$REPO_DIR/venv/bin/python3" "$REPO_DIR/skills/status/cmd.py" "${@:2}"
        ;;
    demo)
        bash "$SCRIPT_DIR/demo.sh" "${@:2}"
        ;;
    overnight)
        # Verify the Mac is ready for unattended overnight background work.
        # Correct approach: AC power + Power Nap. No caffeinate needed.
        echo "🌙 Overnight readiness check"
        echo ""

        # 1. AC power?
        if pmset -g batt 2>/dev/null | grep -q "AC Power"; then
            echo "  ✅ AC power — plugged in"
        else
            echo "  ⚠️  On battery — plug in before closing lid"
            echo "     Power Nap only fires launchd agents reliably on AC power."
        fi

        # 2. Power Nap enabled?
        PN=$(pmset -g | awk '/powernap/{print $2}')
        if [ "$PN" = "1" ]; then
            echo "  ✅ Power Nap enabled"
        else
            echo "  ⚠️  Power Nap disabled — enabling now..."
            sudo pmset -a powernap 1 && echo "  ✅ Power Nap enabled" || echo "  ✗  Could not enable (run: sudo pmset -a powernap 1)"
        fi

        # 3. Launchd agents loaded?
        for AGENT in com.aaka.queueworker com.aaka.nudgeengine com.aaka.calendarsync; do
            if launchctl list "$AGENT" &>/dev/null; then
                echo "  ✅ $AGENT loaded"
            else
                echo "  ⚠️  $AGENT not loaded"
            fi
        done

        echo ""
        echo "Close the lid — Power Nap will wake the Mac every 15–30 min to run agents."
        echo "No caffeinate, no battery drain, no display needed."
        ;;
    help|--help|-h|*)
        echo "Usage: bash admin/aaka.sh [diagnose|deploy|test|rebuild|exec|skills|auth|sync|reauth|cal|queue|status|tunnel-claw|demo|overnight]"
        echo ""
        echo "  setup        AI-native non-interactive setup [--config JSON] [--check] [--reset]"
        echo "  diagnose     Full health check — environment, deps, config, queue, &Away/&Home"
        echo "  deploy       Install deps, configure, and launch (interactive, branches on vps vs local)"
        echo "  test         Smoke tests — gateway import, config load, dry-run intents, queue"
        echo "  rebuild      Quick VPS redeploy: tear down, pull, rebuild, smoke test (VPS only)"
        echo "  exec         &Home command shortcuts (poll-sensor, …)"
        echo "  skills       Skill inventory — descriptions, permissions, deps, stats"
        echo "  auth         Token validity, active senders, security report [--push-tokens]"
        echo "  sync         Calendar sync status report [--run] [--watch]"
        echo "  reauth       Re-authorize a Google OAuth token — interactive menu (&away VPS or &home members)"
        echo "  cal          Verify calendar access — list all configured calendars with ✓/✗"
        echo "  queue        &Home queue status — item counts, recent items, last poll log"
        echo "  status       Shared status sub-commands: code | queue | llm"
        echo "  tunnel-claw  SSH tunnel to OpenClaw dashboard on VPS [local-port, default: 18789]"
        echo "  vps-telegram Diagnose Telegram channel on VPS; offer permission repair + restart"
        echo "  vps-whatsapp Diagnose WhatsApp channel on VPS; restart or clear session for re-pair
  demo         Launch demo REPL (Ash-Kaa family, no Docker, no API keys needed) [--reset]
  overnight     Check AC power + Power Nap ready for unattended background work"
        ;;
esac
