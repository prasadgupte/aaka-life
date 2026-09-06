#!/usr/bin/env bash
# admin/deploy.sh — Install deps, configure, launch
# Run: bash admin/deploy.sh
#
# Branches on INSTANCE (vps | local) detected by lib/detect.sh.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/detect.sh"

echo -e "${BOLD}Aaka Deploy — $INSTANCE ($MODE)${NC}"
echo "================================"

# ── Shared: runs on both VPS and Mac ──────────────────────────────────────
header "1. Create Config Directory Structure"
mkdir -p "$AAKA_CONFIG_DIR/config"
mkdir -p "$AAKA_CONFIG_DIR/tokens"
mkdir -p "$AAKA_CONFIG_DIR/data/queue"
mkdir -p "$AAKA_CONFIG_DIR/data/calendar"
mkdir -p "$AAKA_CONFIG_DIR/logs"
ok "Directories created under $AAKA_CONFIG_DIR"

# Seed aaka.yaml from sample if missing
SAMPLE="$REPO_DIR/config/sample/aaka.yaml.example"
TARGET="$AAKA_CONFIG_DIR/config/aaka.yaml"
if [[ ! -f "$TARGET" && -f "$SAMPLE" ]]; then
  cp "$SAMPLE" "$TARGET"
  warn "Seeded $TARGET from sample — fill in your values before running."
fi
# ──────────────────────────────────────────────────────────────────────────

# Pin the signal-cli version we install on Linux. Homebrew tracks its own on the
# Mac. Override with SIGNAL_CLI_VERSION=… to test a newer build.
SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:-0.14.7}"

ensure_signal_cli() {
    # Install signal-cli if it is missing. Signal has no bot API, so the channel
    # is driven by this unofficial client against a REAL account — the binary is
    # a hard dependency of the Signal channel, not an optional extra. Returns 0
    # when signal-cli is available afterwards, 1 when the caller must skip.
    if command -v signal-cli &>/dev/null; then
        ok "signal-cli present: $(signal-cli --version 2>/dev/null | head -1)"
        return 0
    fi
    if [ "$INSTANCE" = "vps" ] || [ "$(uname -s)" = "Linux" ]; then
        info "Installing signal-cli $SIGNAL_CLI_VERSION + JRE (apt)…"
        apt-get update -qq
        # Headless JRE only — signal-cli is a JVM app and needs no desktop stack.
        apt-get install -y -qq openjdk-21-jre-headless curl >/dev/null 2>&1 || {
            fail "apt could not install openjdk-21-jre-headless"; return 1; }
        local _url="https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
        local _tmp; _tmp="$(mktemp -d)"
        if ! curl -fsSL "$_url" -o "$_tmp/signal-cli.tar.gz"; then
            fail "download failed: $_url"; rm -rf "$_tmp"; return 1
        fi
        rm -rf /opt/signal-cli
        mkdir -p /opt/signal-cli
        tar -xzf "$_tmp/signal-cli.tar.gz" -C /opt/signal-cli --strip-components=1
        ln -sf /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli
        rm -rf "$_tmp"
    else
        if ! command -v brew &>/dev/null; then
            fail "Homebrew not found — install it, or install signal-cli by hand, then re-run deploy."
            return 1
        fi
        info "Installing signal-cli via Homebrew (pulls its own JRE)…"
        brew install signal-cli >/dev/null 2>&1 || { fail "brew install signal-cli failed"; return 1; }
    fi
    if command -v signal-cli &>/dev/null; then
        ok "signal-cli installed: $(signal-cli --version 2>/dev/null | head -1)"
        return 0
    fi
    fail "signal-cli still not on PATH after install"
    return 1
}

if [ "$INSTANCE" = "vps" ]; then
    # ── VPS ───────────────────────────────────────────────────────────────

    header "2. Install Docker CE"
    if command -v docker &>/dev/null; then
        ok "Docker already installed: $(docker --version)"
    else
        info "Installing Docker CE via apt…"
        apt-get update -qq
        apt-get install -y -qq ca-certificates curl gnupg lsb-release
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
            gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
            https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
            tee /etc/apt/sources.list.d/docker.list > /dev/null
        apt-get update -qq
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
        ok "Docker CE installed"
    fi

    header "3. Clone / Update Repo"
    # Set AAKA_REPO_URL to your fork URL before running, e.g.:
    #   export AAKA_REPO_URL=https://github.com/your-fork/aaka.git
    REPO_URL="${AAKA_REPO_URL:-https://github.com/prasadgupte/aaka-life.git}"
    if [ -d "$REPO_DIR/.git" ]; then
        info "Repo exists — pulling latest…"
        git -C "$REPO_DIR" pull --ff-only
        ok "Repo updated at $REPO_DIR"
    else
        info "Cloning repo to $REPO_DIR…"
        if [ -n "${GITHUB_PAT:-}" ]; then
            git clone "https://${GITHUB_PAT}@${REPO_URL#https://}" "$REPO_DIR"
        elif [ "${CLONE_VIA_SSH:-}" = "1" ]; then
            git clone "$REPO_URL" "$REPO_DIR"
        else
            echo "ERROR: Set GITHUB_PAT or CLONE_VIA_SSH=1" >&2
            exit 1
        fi
        ok "Repo cloned to $REPO_DIR"
    fi

    header "4. Configure .env"
    # Canonical location: $AAKA_CONFIG_DIR/.env (backed up with config dir)
    # Repo symlink:       $REPO_DIR/.env → $AAKA_CONFIG_DIR/.env
    CANONICAL_ENV="$AAKA_CONFIG_DIR/.env"
    REPO_ENV="$REPO_DIR/.env"

    if [ -f "$CANONICAL_ENV" ]; then
        ok ".env found at $CANONICAL_ENV (from backup)"
    else
        info "No .env found — entering interactive setup…"
        read -rp "  TELEGRAM_BOT_TOKEN:  " tok
        read -rp "  TELEGRAM_GROUP_ID:   " tg_group
        read -rp "  GEMINI_API_KEY:      " gemini
        read -rp "  WHATSAPP_PHONE (e.g. 491700000000@s.whatsapp.net): " wa_phone
        read -rp "  WHATSAPP_GROUP_JID (e.g. 120363000000000000@g.us):  " wa_group
        cat > "$CANONICAL_ENV" <<ENVEOF
TELEGRAM_BOT_TOKEN=${tok}
TELEGRAM_GROUP_ID=${tg_group}
GEMINI_API_KEY=${gemini}
WHATSAPP_PHONE=${wa_phone}
WHATSAPP_GROUP_JID=${wa_group}
ENVEOF
        chmod 600 "$CANONICAL_ENV"
        ok ".env written to $CANONICAL_ENV"
    fi

    # Symlink into repo dir (gitignored) so docker compose picks it up
    if [ -L "$REPO_ENV" ] && [ "$(readlink "$REPO_ENV")" = "$CANONICAL_ENV" ]; then
        ok ".env symlink already correct"
    else
        ln -sf "$CANONICAL_ENV" "$REPO_ENV"
        ok ".env symlinked: $REPO_ENV → $CANONICAL_ENV"
    fi

    header "5. Build + Launch &Away"

    cd "$REPO_DIR"
    git pull --ff-only
    docker compose -f docker-compose.prod.yml up -d --build sensor
    ok "&Away launched"
    info "Check logs: docker logs aaka-sensor -f"

    header "6. Initialize Queue DB"
    SCHEMA="$REPO_DIR/aaka_queue/schema.sql"
    if command -v sqlite3 &>/dev/null; then
        sqlite3 "$AAKA_CONFIG_DIR/data/queue/butler.db" < "$SCHEMA"
        ok "Queue DB initialized: $AAKA_CONFIG_DIR/data/queue/butler.db"
    else
        warn "sqlite3 not found — DB will be created on first message (run: apt-get install -y sqlite3)"
    fi

    header "7. Authorize Mac (&Home) SSH Key"
    AUTH_KEYS="$HOME/.ssh/authorized_keys"
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    touch "$AUTH_KEYS" && chmod 600 "$AUTH_KEYS"

    if grep -q "aaka-executor" "$AUTH_KEYS" 2>/dev/null; then
        ok "Mac executor key already authorized"
        grep "aaka-executor" "$AUTH_KEYS" | sed 's/^/    /'
    else
        echo ""
        info "Paste the Mac executor pubkey (contents of ~/.ssh/aaka_executor.pub on Mac)."
        info "Press Enter, paste the key line, then Ctrl-D:"
        echo ""
        PASTED_KEY=$(cat)
        if [ -n "$PASTED_KEY" ]; then
            echo "$PASTED_KEY" >> "$AUTH_KEYS"
            ok "Executor pubkey appended to $AUTH_KEYS"
        else
            warn "No key pasted — skipping. Add manually: echo '<pubkey>' >> $AUTH_KEYS"
        fi
    fi

    header "8. Signal channel (&Away)"
    # The Signal daemon belongs on whichever host flushes the outbox, and that is
    # this one: sensor/flush_outbox.py runs from cron here, so a Mac-only daemon
    # would leave every queued-intent reply unsent. Hence sensor is the default
    # placement and the units live here.
    _EC="${ENABLED_CHANNELS:-}"
    if [ -z "$_EC" ] && [ -f "$AAKA_CONFIG_DIR/.env" ]; then
        _EC="$(grep -E '^ENABLED_CHANNELS=' "$AAKA_CONFIG_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\042\047 ')"
    fi
    _EC="${_EC:-telegram}"
    _SP="${SIGNAL_PLACEMENT:-}"
    if [ -z "$_SP" ] && [ -f "$AAKA_CONFIG_DIR/.env" ]; then
        _SP="$(grep -E '^SIGNAL_PLACEMENT=' "$AAKA_CONFIG_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\042\047 ')"
    fi
    _SP="${_SP:-sensor}"
    _SA="${SIGNAL_ACCOUNT:-}"
    if [ -z "$_SA" ] && [ -f "$AAKA_CONFIG_DIR/.env" ]; then
        _SA="$(grep -E '^SIGNAL_ACCOUNT=' "$AAKA_CONFIG_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\042\047 ')"
    fi

    if ! echo "$_EC" | grep -q "signal"; then
        info "Signal not in ENABLED_CHANNELS — skipping (Telegram-only &Away)."
    elif [ "$_SP" != "sensor" ]; then
        info "SIGNAL_PLACEMENT=$_SP — the daemon runs on the Mac; nothing to install here."
    elif ! ensure_signal_cli; then
        warn "signal-cli unavailable — Signal services NOT installed on &Away."
    elif [ -z "$_SA" ]; then
        warn "SIGNAL_ACCOUNT not set in $AAKA_CONFIG_DIR/.env — units NOT installed."
        info "Register a DEDICATED number first (never one already on a phone —"
        info "that would deregister Signal there):"
        info "  signal-cli -a +E164 register       # add --voice for a landline"
        info "  signal-cli -a +E164 verify CODE"
        info "Then set SIGNAL_ACCOUNT=+E164 and re-run this script."
    else
        for unit in aaka-signal-cli aaka-signal-poller; do
            U_SRC="$REPO_DIR/deploy/$unit.service"
            if [ -f "$U_SRC" ]; then
                sed -e "s|/opt/aaka-repo|$REPO_DIR|g" \
                    -e "s|/opt/aaka-config|$AAKA_CONFIG_DIR|g" \
                    "$U_SRC" > "/etc/systemd/system/$unit.service"
                ok "installed /etc/systemd/system/$unit.service"
            else
                warn "unit not found: $U_SRC"
            fi
        done
        systemctl daemon-reload
        systemctl enable --now aaka-signal-cli aaka-signal-poller 2>/dev/null \
            && ok "aaka-signal-cli + aaka-signal-poller enabled and started" \
            || warn "systemctl enable/start failed — check: journalctl -u aaka-signal-cli -n 50"
        if ! signal-cli -a "$_SA" listAccounts &>/dev/null; then
            warn "$_SA is not registered yet on this host — the daemon will fail until it is."
            info "  signal-cli -a $_SA register   (then verify CODE)"
        fi
    fi

else
    # ── Local Mac ──────────────────────────────────────────────────────────

    header "2. Install Python Dependencies"
    VENV_DIR="$REPO_DIR/venv"
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        ok "Virtualenv created at $VENV_DIR"
    else
        ok "Virtualenv already exists at $VENV_DIR"
    fi
    "$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
    ok "Python deps installed into venv"

    header "3. SSH Key Setup (Executor → VPS)"
    EXECUTOR_KEY="$HOME/.ssh/aaka_executor"
    SSH_CONFIG="$HOME/.ssh/config"

    # 3a. Generate key if not exists (no passphrase — required for launchd/unattended use)
    if [ -f "$EXECUTOR_KEY" ]; then
        ok "SSH key already exists: $EXECUTOR_KEY"
    else
        ssh-keygen -t ed25519 -f "$EXECUTOR_KEY" -N "" -C "aaka-executor"
        ok "SSH key generated: $EXECUTOR_KEY"
    fi

    # 3b. Add Host block to SSH config (idempotent)
    # AAKA_VPS_HOST defaults to "aaka-away"; AAKA_VPS_IP must be set in
    # $AAKA_CONFIG_DIR/.env or the environment.
    if [ -z "${AAKA_VPS_IP:-}" ]; then
        warn "AAKA_VPS_IP is not set — skipping SSH config block creation"
        info "  Set AAKA_VPS_IP in $AAKA_CONFIG_DIR/.env and re-run deploy."
    elif grep -q "Host ${AAKA_VPS_HOST}" "$SSH_CONFIG" 2>/dev/null; then
        ok "SSH config already has Host ${AAKA_VPS_HOST} block"
    else
        touch "$SSH_CONFIG" && chmod 600 "$SSH_CONFIG"
        [ -s "$SSH_CONFIG" ] && echo "" >> "$SSH_CONFIG"
        cat >> "$SSH_CONFIG" <<SSHBLOCK
Host ${AAKA_VPS_HOST}
    HostName ${AAKA_VPS_IP}
    User root
    IdentityFile ~/.ssh/aaka_executor
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    AddKeysToAgent yes
    UseKeychain yes
SSHBLOCK
        ok "SSH config block added for Host ${AAKA_VPS_HOST}"
    fi

    # 3c. Test connectivity (non-fatal — VPS auth may not be set up yet)
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "${AAKA_VPS_HOST}" "echo ok" &>/dev/null 2>&1; then
        ok "SSH connection to ${AAKA_VPS_HOST}: OK"
    else
        warn "SSH to ${AAKA_VPS_HOST} not yet authorized — complete VPS side to finish setup"
        info "Mac executor pubkey to authorize on VPS:"
        echo ""
        cat "$EXECUTOR_KEY.pub"
        echo ""
        info "On VPS: bash admin/aaka.sh deploy  (step 7 will prompt for this key)"
        if [ -n "${AAKA_VPS_IP:-}" ]; then
            info "Or one-liner if you have VPS password access:"
            info "  cat $EXECUTOR_KEY.pub | ssh root@${AAKA_VPS_IP} 'cat >> ~/.ssh/authorized_keys'"
        fi
    fi

    header "4. Install / Reload Launchd Plist"
    PLIST_SRC="$REPO_DIR/executor/com.aaka.queueworker.plist"
    PLIST_DST="$HOME/Library/LaunchAgents/com.aaka.queueworker.plist"
    # Substitute $AAKA_BASE placeholder before installing
    sed "s|\${AAKA_BASE}|$REPO_DIR|g" "$PLIST_SRC" > "$PLIST_DST"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    launchctl load "$PLIST_DST"
    ok "launchd job com.aaka.queueworker loaded (daemon mode)"
    info "Logs: $AAKA_CONFIG_DIR/logs/queueworker.log"

    # Remove legacy dbsync plist if present (sync now runs inside queueworker)
    DBSYNC_DST="$HOME/Library/LaunchAgents/com.aaka.dbsync.plist"
    if [ -f "$DBSYNC_DST" ]; then
        launchctl unload "$DBSYNC_DST" 2>/dev/null || true
        rm -f "$DBSYNC_DST"
        ok "Removed legacy com.aaka.dbsync (sync now built into queueworker)"
    fi

    # Install calendar sync launchd plist
    SYNC_SRC="$REPO_DIR/executor/com.aaka.calendarsync.plist"
    SYNC_DEST="$HOME/Library/LaunchAgents/com.aaka.calendarsync.plist"
    if [[ -f "$SYNC_SRC" ]]; then
        sed "s|\${AAKA_BASE}|$REPO_DIR|g" "$SYNC_SRC" > "$SYNC_DEST"
        launchctl unload "$SYNC_DEST" 2>/dev/null || true
        launchctl load "$SYNC_DEST"
        ok "com.aaka.calendarsync installed and loaded"
        info "Logs: $AAKA_CONFIG_DIR/logs/calendarsync.log"
    else
        warn "Calendar sync plist not found: $SYNC_SRC"
    fi

    # aaka Tools runner (executor-placed tools, cron-checked every 60s)
    TR_SRC="$REPO_DIR/executor/com.aaka.toolrunner.plist"
    TR_DST="$HOME/Library/LaunchAgents/com.aaka.toolrunner.plist"
    if [[ -f "$TR_SRC" ]]; then
        sed -e "s|\${AAKA_BASE}|$REPO_DIR|g" \
            -e "s|\${AAKA_CONFIG_DIR}|$AAKA_CONFIG_DIR|g" \
            -e "s|\${PYTHON_BIN}|$REPO_DIR/venv/bin/python3|g" \
            "$TR_SRC" > "$TR_DST"
        launchctl unload "$TR_DST" 2>/dev/null || true
        launchctl load "$TR_DST"
        ok "com.aaka.toolrunner installed and loaded (aaka Tools scheduler)"
    fi

    # WhatsApp sidecar + inbound receiver — only when whatsapp is enabled. Makes
    # WhatsApp always-on like Telegram: no manual `node`/`uvicorn` to keep running.
    # Read ENABLED_CHANNELS from the canonical .env (not just the shell env) — the
    # value the user set in .env is the source of truth for a fresh deploy.
    _EC="${ENABLED_CHANNELS:-}"
    if [ -z "$_EC" ] && [ -f "$AAKA_CONFIG_DIR/.env" ]; then
        _EC="$(grep -E '^ENABLED_CHANNELS=' "$AAKA_CONFIG_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\'' ')"
    fi
    _EC="${_EC:-telegram}"
    if echo "$_EC" | grep -q "whatsapp"; then
        NODE_BIN="$(command -v node || true)"
        PYTHON_BIN="$REPO_DIR/venv/bin/python3"
        if [ -z "$NODE_BIN" ]; then
            warn "node not found on PATH — WhatsApp sidecar service NOT installed. Install Node 20+ and re-run deploy."
        elif [ ! -x "$PYTHON_BIN" ]; then
            warn "venv python missing ($PYTHON_BIN) — WhatsApp receiver NOT installed. Create the venv and re-run deploy."
        else
            if [ ! -d "$REPO_DIR/wa-sidecar/node_modules" ]; then
                info "Installing wa-sidecar Node deps (one-time)…"
                ( cd "$REPO_DIR/wa-sidecar" && npm install >/dev/null 2>&1 ) \
                    && ok "wa-sidecar deps installed" || warn "npm install failed in wa-sidecar — install manually"
            fi
            # Shared secret for POST /inbound — both plists must carry the SAME
            # value or the sidecar's forwards 401. Read from the canonical .env;
            # empty is valid and keeps the old no-auth localhost behaviour.
            _WA_SECRET="$(grep -E '^WA_INBOUND_SECRET=' "$REPO_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\042\047 ')"
            for wa in wasidecar wasidecar_receiver; do
                WA_SRC="$REPO_DIR/executor/com.aaka.$wa.plist"
                WA_DST="$HOME/Library/LaunchAgents/com.aaka.$wa.plist"
                if [ -f "$WA_SRC" ]; then
                    sed -e "s|\${AAKA_BASE}|$REPO_DIR|g" \
                        -e "s|\${AAKA_CONFIG_DIR}|$AAKA_CONFIG_DIR|g" \
                        -e "s|\${NODE_BIN}|$NODE_BIN|g" \
                        -e "s|\${PYTHON_BIN}|$PYTHON_BIN|g" \
                        -e "s|\${WA_INBOUND_SECRET}|$_WA_SECRET|g" \
                        "$WA_SRC" > "$WA_DST"
                    launchctl unload "$WA_DST" 2>/dev/null || true
                    launchctl load "$WA_DST"
                    ok "com.aaka.$wa installed and loaded"
                else
                    warn "WhatsApp plist not found: $WA_SRC"
                fi
            done
            info "WhatsApp always-on: sidecar :18792 + receiver :18793. Pair once at http://127.0.0.1:18792/"
        fi
    else
        info "WhatsApp not in ENABLED_CHANNELS — skipping sidecar services (Telegram-only install)."
    fi

    # Signal — signal-cli JSON-RPC daemon + poller. Only when signal is enabled.
    # Placement matters: replies to QUEUED intents are flushed from cron on the
    # VPS, so the daemon belongs next to that flusher (SIGNAL_PLACEMENT=sensor,
    # the default → deploy/aaka-signal-cli.service + aaka-signal-poller.service).
    # These Mac launchd agents are for SIGNAL_PLACEMENT=executor only.
    if echo "$_EC" | grep -q "signal"; then
        _SP="${SIGNAL_PLACEMENT:-}"
        if [ -z "$_SP" ] && [ -f "$AAKA_CONFIG_DIR/.env" ]; then
            _SP="$(grep -E '^SIGNAL_PLACEMENT=' "$AAKA_CONFIG_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\'' ')"
        fi
        _SP="${_SP:-sensor}"
        _SA="${SIGNAL_ACCOUNT:-}"
        if [ -z "$_SA" ] && [ -f "$AAKA_CONFIG_DIR/.env" ]; then
            _SA="$(grep -E '^SIGNAL_ACCOUNT=' "$AAKA_CONFIG_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\'' ')"
        fi
        SIGNAL_CLI_BIN="$(command -v signal-cli || true)"
        PYTHON_BIN="$REPO_DIR/venv/bin/python3"
        if [ "$_SP" != "executor" ]; then
            info "SIGNAL_PLACEMENT=$_SP — signal-cli runs on the VPS. Install deploy/aaka-signal-cli.service + deploy/aaka-signal-poller.service there; skipping Mac launchd agents."
        elif [ -z "$SIGNAL_CLI_BIN" ] && { ensure_signal_cli; SIGNAL_CLI_BIN="$(command -v signal-cli || true)"; [ -z "$SIGNAL_CLI_BIN" ]; }; then
            warn "signal-cli unavailable — Signal services NOT installed."
        elif [ -z "$_SA" ]; then
            warn "SIGNAL_ACCOUNT not set in .env — Signal services NOT installed. Add SIGNAL_ACCOUNT=+E.164 (a dedicated number) and re-run deploy."
        elif [ ! -x "$PYTHON_BIN" ]; then
            warn "venv python missing ($PYTHON_BIN) — Signal poller NOT installed. Create the venv and re-run deploy."
        else
            for sg in signalcli signalpoller; do
                SG_SRC="$REPO_DIR/executor/com.aaka.$sg.plist"
                SG_DST="$HOME/Library/LaunchAgents/com.aaka.$sg.plist"
                if [ -f "$SG_SRC" ]; then
                    sed -e "s|\${AAKA_BASE}|$REPO_DIR|g" \
                        -e "s|\${AAKA_CONFIG_DIR}|$AAKA_CONFIG_DIR|g" \
                        -e "s|\${SIGNAL_CLI_BIN}|$SIGNAL_CLI_BIN|g" \
                        -e "s|\${SIGNAL_ACCOUNT}|$_SA|g" \
                        -e "s|\${PYTHON_BIN}|$PYTHON_BIN|g" \
                        "$SG_SRC" > "$SG_DST"
                    launchctl unload "$SG_DST" 2>/dev/null || true
                    launchctl load "$SG_DST"
                    ok "com.aaka.$sg installed and loaded"
                else
                    warn "Signal plist not found: $SG_SRC"
                fi
            done
            info "Signal always-on: signal-cli daemon :18794 (loopback) + poller. Verify with admin/diagnose.sh."
        fi
    else
        info "Signal not in ENABLED_CHANNELS — skipping signal-cli services."
    fi

    header "Step 5 — Create vault scaffold"
    # Vault roots are per-member (vault_path in aaka.yaml) + shared vault
    # Scaffold is created on the filesystem; gDrive sync handles backup
    # Use the venv python — it has pyyaml (bare python3 usually doesn't).
    PY="$REPO_DIR/venv/bin/python3"; [ -x "$PY" ] || PY="python3"
    "$PY" - <<'PYEOF'
import sys, os
sys.path.insert(0, os.environ.get("AAKA_BASE", "."))
try:
    import aaka_config
    member_dirs = ["00-Inbox", "00-ID", "01-Projects", "02-Areas", "03-Resources",
                   "04-Notes", "05-Travel", "90-Archives", "95-Backup",
                   "99-System/contexts", "99-System/.indexes", "99-System/.memory"]
    shared_dirs = ["00-Inbox", "01-Projects", "02-Areas", "04-Notes",
                   "90-Archives", "95-Backup", "99-System/.indexes", "99-System/.memory"]
    for m in aaka_config.members():
        vp = m.get("vault_path")
        if not vp:
            continue
        for d in member_dirs:
            os.makedirs(f"{vp}/{d}", exist_ok=True)
    shared_vp = aaka_config.SHARED_VAULT_PATH
    for d in shared_dirs:
        os.makedirs(f"{shared_vp}/{d}", exist_ok=True)
    # Mac only: create data/notes symlinks to member vaults
    if os.uname().sysname == "Darwin":
        config_dir = str(aaka_config.CONFIG_DIR)
        notes_base = f"{config_dir}/data/notes"
        os.makedirs(notes_base, exist_ok=True)
        for m in aaka_config.members():
            mid = m.get("id")
            vp = m.get("vault_path")
            if not mid or not vp:
                continue
            link = f"{notes_base}/{mid}"
            target = f"{vp}/04-Notes"
            if not os.path.exists(link):
                os.symlink(target, link)
                print(f"Symlink: {link} -> {target}")
        shared_link = f"{notes_base}/_shared"
        shared_target = f"{shared_vp}/04-Notes"
        if not os.path.exists(shared_link):
            os.symlink(shared_target, shared_link)
            print(f"Symlink: {shared_link} -> {shared_target}")
    print("Vault scaffold done")
except Exception as e:
    print(f"Warning: vault scaffold failed: {e}", file=sys.stderr)
PYEOF
    ok "Vault scaffold created"

    header "Step 6 — Migrate SOUL.md to vault"
    SOUL_SRC="$AAKA_BASE/contexts/family/SOUL.md"
    # Resolve the admin member's vault_path (was previously an unbound $VAULT).
    VAULT="$("$PY" -c "import sys; sys.path.insert(0,'$AAKA_BASE'); import aaka_config; m=next((x for x in aaka_config.members() if x.get('admin')), None); print((m or {}).get('vault_path',''))" 2>/dev/null || echo "")"
    if [ -z "$VAULT" ]; then
      warn "no admin member vault_path in aaka.yaml — skipping SOUL.md migration"
    elif [ ! -f "$SOUL_SRC" ]; then
      warn "SOUL.md not found at $SOUL_SRC — skipping"
    else
      SOUL_DST="$VAULT/99-System/contexts/family.md"
      mkdir -p "$(dirname "$SOUL_DST")"
      if [ ! -f "$SOUL_DST" ]; then
        cp "$SOUL_SRC" "$SOUL_DST" && ok "Copied SOUL.md → $SOUL_DST" \
          || fail "Failed to copy SOUL.md"
      else
        ok "family.md already present — skipped"
      fi
    fi
fi

echo ""
ok "Deploy complete. Run: bash admin/aaka.sh diagnose"
