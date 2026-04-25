#!/usr/bin/env bash
# sync.sh — calendar sync status report
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/detect.sh"

DO_RUN=false
DO_WATCH=false
for arg in "$@"; do
    [[ "$arg" == "--run" ]] && DO_RUN=true
    [[ "$arg" == "--watch" ]] && DO_WATCH=true
done

PYTHON="${REPO_DIR}/venv/bin/python3"
VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"
CAL_DIR="$AAKA_CONFIG_DIR/data/calendar"
LOG_DIR="$AAKA_CONFIG_DIR/logs"

echo -e "${BOLD}Calendar Sync Status${NC}"
echo "===================="

# Helper: format age
age_label() {
    local secs=$1
    if (( secs < 3600 )); then
        echo "${secs}s ago"
    elif (( secs < 86400 )); then
        echo "$(( secs/3600 ))h ago"
    else
        echo "$(( secs/86400 ))d ago"
    fi
}

file_age() {
    local f=$1
    if [[ ! -f "$f" ]]; then echo -1; return; fi
    local mtime now
    mtime=$(stat -f %m "$f" 2>/dev/null || echo 0)
    now=$(date +%s)
    echo $(( now - mtime ))
}

# 1. Calendar files
header "1. Calendar Files"
for fname in today.md weekly.md; do
    fpath="$CAL_DIR/$fname"
    age=$(file_age "$fpath")
    if (( age < 0 )); then
        fail "$fname  MISSING ($fpath)"
    elif (( age < 3600 )); then
        ok "$fname  $(age_label $age)"
    elif (( age < 86400 )); then
        warn "$fname  STALE — $(age_label $age)"
    else
        fail "$fname  VERY STALE — $(age_label $age)"
    fi
    # Migration hint: check old token path
    OLD_TOKEN="$AAKA_CONFIG_DIR/config/token_aakash.json"
    if [[ -f "$OLD_TOKEN" ]] && [[ ! -f "$AAKA_CONFIG_DIR/tokens/token_aakash.json" ]]; then
        warn "  Migration needed: token found at old path $OLD_TOKEN — move to $AAKA_CONFIG_DIR/tokens/"
    fi
done

# 2. Audit log
header "2. Audit Log"
AUDIT="$LOG_DIR/audit.log"
if [[ -f "$AUDIT" ]]; then
    last=$(tail -1 "$AUDIT")
    ok "Last entry: $last"
else
    warn "audit.log missing — sync may never have run ($AUDIT)"
fi

# 3. Executor heartbeat
header "3. &Home Heartbeat"
LAST_CHECK="$LOG_DIR/last_executor_check"
if [[ -f "$LAST_CHECK" ]]; then
    age=$(file_age "$LAST_CHECK")
    if (( age < 300 )); then
        ok "&Home active — $(age_label $age)"
    else
        warn "&Home last seen $(age_label $age) ago — may be down"
    fi
else
    warn "last_executor_check not found — &Home may not be running"
fi

# 4. launchd jobs (local only)
if [ "$INSTANCE" = "local" ]; then
    header "4. launchd Jobs"
    for job in com.aaka.queueworker com.aaka.calendarsync; do
        result=$(launchctl list "$job" 2>/dev/null || echo "NOT_LOADED")
        if [[ "$result" == "NOT_LOADED" ]]; then
            fail "$job  NOT LOADED  → run: bash admin/aaka.sh deploy"
        else
            pid=$(echo "$result" | awk 'NR==1{print $1}')
            if [[ "$pid" =~ ^[0-9]+$ ]]; then
                ok "$job  running (pid=$pid)"
            else
                ok "$job  loaded (not currently running)"
            fi
        fi
    done
fi

# 5. VPS connectivity (local only)
if [ "$INSTANCE" = "local" ]; then
    header "5. VPS Connectivity"
    if ssh -o ConnectTimeout=5 -o BatchMode=yes ${VPS_HOST} echo ok &>/dev/null; then
        ok "${VPS_HOST} reachable"
        sensor_status=$(ssh ${VPS_HOST} "docker ps --filter name=sensor --format '{{.Status}}'" 2>/dev/null || echo "unknown")
        if [[ -n "$sensor_status" ]] && [[ "$sensor_status" != "unknown" ]]; then
            ok "sensor container: $sensor_status"
        else
            warn "sensor container not found or SSH docker query failed"
        fi
    else
        warn "${VPS_HOST} unreachable (SSH timeout)"
    fi
fi

# 6. VPS Calendar Files (local only)
if [ "$INSTANCE" = "local" ]; then
    header "6. VPS Calendar Files"
    if ssh -o ConnectTimeout=5 -o BatchMode=yes ${VPS_HOST} echo ok &>/dev/null 2>&1; then
        now=$(date +%s)
        for fname in today.md weekly.md weekly_events.json; do
            mtime=$(ssh ${VPS_HOST} "stat -c %Y /opt/aaka-config/data/calendar/$fname 2>/dev/null || echo 0")
            age=$(( (now - mtime) / 60 ))
            if   [[ $mtime -eq 0 ]];   then fail "[$fname] missing on VPS"
            elif [[ $age -lt 60 ]];    then ok   "[$fname] age=${age}m"
            elif [[ $age -lt 1440 ]];  then warn "[$fname] stale — age=${age}m"
            else                            fail "[$fname] very stale — age=${age}m"
            fi
        done
        # Birthday window (separate dir — data/contacts/)
        bday_mtime=$(ssh ${VPS_HOST} "stat -c %Y /opt/aaka-config/data/contacts/birthday_window.json 2>/dev/null || echo 0")
        bday_age=$(( (now - bday_mtime) / 60 ))
        if   [[ $bday_mtime -eq 0 ]];       then warn "[birthday_window.json] not yet on VPS — queueworker sync will push it"
        elif [[ $bday_age -lt 1440 ]];      then ok   "[birthday_window.json] age=${bday_age}m"
        elif [[ $bday_age -lt 10080 ]];     then warn "[birthday_window.json] stale — age=${bday_age}m"
        else                                     fail  "[birthday_window.json] very stale — age=${bday_age}m"
        fi
    else
        warn "${VPS_HOST} unreachable — cannot check VPS calendar files"
    fi
fi

# 7. GCal API heartbeat (local only)
if [ "$INSTANCE" = "local" ]; then
    header "7. GCal API Heartbeat"
    all_members=$(AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$AAKA_CONFIG_DIR" \
        "$PYTHON" -c "import sys; sys.path.insert(0,'$REPO_DIR'); import aaka_config; print(' '.join(m['id'] for m in aaka_config.auth_members()))" 2>/dev/null)
    if [[ -n "$all_members" ]]; then
        for member in $all_members; do
            hb=$(AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$AAKA_CONFIG_DIR" \
                "$PYTHON" "$REPO_DIR/skills/auth_check.py" --member "$member" --heartbeat 2>/dev/null)
            if [[ -z "$hb" ]]; then
                fail "[$member] auth_check returned no output"
                continue
            fi
            ok_val=$(echo "$hb" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok','false'))" 2>/dev/null)
            lat=$(echo "$hb" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('latency_ms','?'))" 2>/dev/null)
            err=$(echo "$hb" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('error') or '')" 2>/dev/null)
            if [[ "$ok_val" == "True" ]]; then
                ok "[$member] GCal API OK — latency=${lat}ms"
            else
                fail "[$member] GCal API FAILED — $err"
            fi
        done
    else
        warn "No auth members configured — skipping GCal heartbeat"
    fi
fi

# 8. --run trigger (local only)
if $DO_RUN; then
    header "8. Running Sync"
    if [ "$INSTANCE" != "local" ]; then
        warn "--run only available on local instance"
    else
        if $DO_WATCH && [[ -f "$AUDIT" ]]; then
            tail -f "$AUDIT" &
            TAIL_PID=$!
        fi
        AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$AAKA_CONFIG_DIR" AAKA_CONTEXT=family \
            "$PYTHON" "$REPO_DIR/skills/calendar/sidecar_sync.py" && ok "sync completed successfully" || fail "sync exited with error"
        if $DO_WATCH && [[ -n "${TAIL_PID:-}" ]]; then
            kill "$TAIL_PID" 2>/dev/null || true
        fi
    fi
fi
