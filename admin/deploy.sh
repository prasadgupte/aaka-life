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
        read -rp "  WHATSAPP_GROUP_JID (e.g. 120363424460011185@g.us):  " wa_group
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

    # WhatsApp sidecar + inbound receiver — only when whatsapp is enabled. Makes
    # WhatsApp always-on like Telegram: no manual `node`/`uvicorn` to keep running.
    if echo "${ENABLED_CHANNELS:-telegram}" | grep -q "whatsapp"; then
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
            for wa in wasidecar wasidecar_receiver; do
                WA_SRC="$REPO_DIR/executor/com.aaka.$wa.plist"
                WA_DST="$HOME/Library/LaunchAgents/com.aaka.$wa.plist"
                if [ -f "$WA_SRC" ]; then
                    sed -e "s|\${AAKA_BASE}|$REPO_DIR|g" \
                        -e "s|\${AAKA_CONFIG_DIR}|$AAKA_CONFIG_DIR|g" \
                        -e "s|\${NODE_BIN}|$NODE_BIN|g" \
                        -e "s|\${PYTHON_BIN}|$PYTHON_BIN|g" \
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

    header "Step 5 — Create vault scaffold"
    # Vault roots are per-member (vault_path in aaka.yaml) + shared vault
    # Scaffold is created on the filesystem; gDrive sync handles backup
    python3 - <<'PYEOF'
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
    SOUL_DST="$VAULT/System/contexts/family.md"
    if [ ! -f "$SOUL_DST" ]; then
      cp "$SOUL_SRC" "$SOUL_DST" && ok "Copied SOUL.md → vault/System/contexts/family.md" \
        || fail "Failed to copy SOUL.md"
    else
      ok "family.md already present — skipped"
    fi
fi

echo ""
ok "Deploy complete. Run: bash admin/aaka.sh diagnose"
