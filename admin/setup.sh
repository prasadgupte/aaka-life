#!/usr/bin/env bash
# admin/setup.sh — Non-interactive AI-native setup driver
#
# Usage:
#   bash admin/setup.sh --config /tmp/aaka-setup.json   # full setup (AI path)
#   bash admin/setup.sh --check                         # read-only pre-flight
#   bash admin/setup.sh --config /tmp/aaka-setup.json --check
#   bash admin/setup.sh --config /tmp/aaka-setup.json --reset  # overwrite existing files
#
# Output: one line per step with prefix [OK] [SKIP] [WARN] [FAIL]
# Exit code: 0 if no [FAIL], non-zero otherwise.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

CONFIG_FILE=""
CHECK_ONLY=0
RESET=0
FAIL_COUNT=0

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --check)  CHECK_ONLY=1; shift ;;
    --reset)  RESET=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ── Step helpers ──────────────────────────────────────────────────────────────
STEP=0
TOTAL=8

_step() {
  STEP=$((STEP + 1))
  LABEL="$*"
}

_ok()   { echo "[OK]   ${STEP}/${TOTAL}  ${LABEL}"; }
_skip() { echo "[SKIP] ${STEP}/${TOTAL}  ${LABEL} — $*"; }
_warn() { echo "[WARN] ${STEP}/${TOTAL}  ${LABEL} — $*"; }
_fail() { echo "[FAIL] ${STEP}/${TOTAL}  ${LABEL} — $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# ── Load JSON config via python3 ──────────────────────────────────────────────
_jq() {
  # Usage: _jq '.key' [default]
  local query="$1"
  local default="${2:-}"
  if [[ -z "$CONFIG_FILE" ]]; then
    echo "$default"
    return
  fi
  python3 -c "
import json, sys
try:
    d = json.load(open('${CONFIG_FILE}'))
    keys = '${query}'.lstrip('.').split('.')
    val = d
    for k in keys:
        val = val.get(k, '') if isinstance(val, dict) else ''
    print(val if val != '' else '${default}')
except Exception as e:
    print('${default}')
" 2>/dev/null
}

_jq_list() {
  # Usage: _jq_list '.members' → JSON array as string
  local query="$1"
  if [[ -z "$CONFIG_FILE" ]]; then echo "[]"; return; fi
  python3 -c "
import json
try:
    d = json.load(open('${CONFIG_FILE}'))
    keys = '${query}'.lstrip('.').split('.')
    val = d
    for k in keys:
        val = val.get(k, []) if isinstance(val, dict) else []
    print(json.dumps(val if isinstance(val, list) else []))
except Exception:
    print('[]')
" 2>/dev/null
}

# ── Resolve config dir ────────────────────────────────────────────────────────
AAKA_CONFIG_DIR="${AAKA_CONFIG_DIR:-$HOME/.aaka}"
# Detect VPS — AAKA_VPS_IP is set from JSON config below (or env). If unset,
# this machine is always treated as &home.
VPS_IP="${AAKA_VPS_IP:-}"
CURRENT_IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || echo "unknown")
if [[ -n "$VPS_IP" && "$CURRENT_IP" == "$VPS_IP" ]]; then
  AAKA_CONFIG_DIR="/opt/aaka-config"
fi

echo ""
echo "Aaka Setup — $(date '+%Y-%m-%d %H:%M')"
echo "Config dir:  $AAKA_CONFIG_DIR"
echo "Repo dir:    $REPO_DIR"
[[ -n "$CONFIG_FILE" ]] && echo "Setup JSON:  $CONFIG_FILE"
[[ $CHECK_ONLY -eq 1 ]] && echo "Mode:        check-only (no writes)"
[[ $RESET -eq 1 ]]      && echo "Mode:        reset (overwrites existing)"
echo ""

# ── Step 1: Config directory structure ───────────────────────────────────────
_step "Config directory structure"
if [[ $CHECK_ONLY -eq 1 ]]; then
  if [[ -d "$AAKA_CONFIG_DIR/config" && -d "$AAKA_CONFIG_DIR/tokens" && -d "$AAKA_CONFIG_DIR/data/queue" ]]; then
    _ok
  else
    _fail "directories missing under $AAKA_CONFIG_DIR"
  fi
else
  mkdir -p \
    "$AAKA_CONFIG_DIR/config" \
    "$AAKA_CONFIG_DIR/tokens" \
    "$AAKA_CONFIG_DIR/data/queue" \
    "$AAKA_CONFIG_DIR/data/calendar" \
    "$AAKA_CONFIG_DIR/data/contacts" \
    "$AAKA_CONFIG_DIR/logs"
  _ok
fi

# ── Step 2: aaka.yaml ─────────────────────────────────────────────────────────
_step "aaka.yaml config"
TARGET_YAML="$AAKA_CONFIG_DIR/config/aaka.yaml"

if [[ -f "$TARGET_YAML" && $RESET -eq 0 ]]; then
  _skip "already exists (use --reset to overwrite)"
elif [[ -z "$CONFIG_FILE" ]]; then
  SAMPLE="$REPO_DIR/config/sample/aaka.yaml.example"
  if [[ -f "$SAMPLE" && ! -f "$TARGET_YAML" ]]; then
    [[ $CHECK_ONLY -eq 0 ]] && cp "$SAMPLE" "$TARGET_YAML"
    _warn "seeded from sample — fill in values manually: $TARGET_YAML"
  elif [[ -f "$TARGET_YAML" ]]; then
    _skip "exists, no --config provided to update it"
  else
    _fail "no --config provided and no sample found at $SAMPLE"
  fi
else
  # Generate aaka.yaml from JSON config
  TZ="$(_jq '.timezone' 'UTC')"
  BOT_NAME="$(_jq '.bot_name' 'Aaka')"
  BOT_EMOJI="$(_jq '.bot_emoji' '🌤️')"
  VAULT_PATH="$(_jq '.vault_path' "${HOME}/aaka-vault")"
  MEMBERS_JSON="$(_jq_list '.members')"
  CALS_JSON="$(_jq_list '.calendars')"

  if [[ $CHECK_ONLY -eq 0 ]]; then
    python3 - <<PYEOF
import json, yaml, sys

members_raw = json.loads('''${MEMBERS_JSON}''')
cals_raw    = json.loads('''${CALS_JSON}''')

members = []
for m in members_raw:
    entry = {
        'id':        m.get('id', m.get('namespace', m['name'].lower())),
        'name':      m['name'],
        'role':      m.get('role', 'member'),
        'namespace': m.get('namespace', m['name'].lower()),
    }
    if m.get('telegram_id'): entry['telegram_id'] = str(m['telegram_id'])
    if m.get('whatsapp_phone'): entry['whatsapp_phone'] = m['whatsapp_phone']
    if m.get('email'): entry['email'] = m['email']
    members.append(entry)

calendars = []
for c in cals_raw:
    entry = {'id': c['id'], 'label': c.get('label', c['id']), 'emoji': c.get('emoji', '📅')}
    if 'private' in c: entry['private'] = c['private']
    calendars.append(entry)

cfg = {
    'system': {
        'timezone':   '${TZ}',
        'bot_name':   '${BOT_NAME}',
        'bot_emoji':  '${BOT_EMOJI}',
        'vault_path': '${VAULT_PATH}',
    },
    'members': members,
    'calendar': {
        'default_id': calendars[0]['id'] if calendars else 'primary',
        'calendars': calendars,
    },
}

with open('${TARGET_YAML}', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
print('written')
PYEOF
    _ok
  else
    _ok  # check-only: config JSON present, would generate
  fi
fi

# ── Step 3: .env file ─────────────────────────────────────────────────────────
_step ".env credentials file"
CANONICAL_ENV="$AAKA_CONFIG_DIR/.env"
REPO_ENV="$REPO_DIR/.env"

if [[ -f "$CANONICAL_ENV" && $RESET -eq 0 ]]; then
  _skip "already exists at $CANONICAL_ENV (use --reset to overwrite)"
elif [[ -z "$CONFIG_FILE" ]]; then
  _warn "no --config provided — .env not written; fill in manually: $CANONICAL_ENV"
else
  TG_TOKEN="$(_jq '.telegram_bot_token')"
  TG_GROUP="$(_jq '.telegram_group_id')"
  TG_USER="$(_jq '.telegram_user_id')"
  GEMINI="$(_jq '.gemini_api_key')"
  WA_PHONE="$(_jq '.whatsapp_phone')"
  WA_GROUP="$(_jq '.whatsapp_group_jid')"

  if [[ -z "$TG_TOKEN" || -z "$GEMINI" ]]; then
    _fail "telegram_bot_token and gemini_api_key are required in config JSON"
  else
    if [[ $CHECK_ONLY -eq 0 ]]; then
      cat > "$CANONICAL_ENV" <<ENVEOF
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_GROUP_ID=${TG_GROUP}
TELEGRAM_USER_ID=${TG_USER}
GEMINI_API_KEY=${GEMINI}
WHATSAPP_PHONE=${WA_PHONE}
WHATSAPP_GROUP_JID=${WA_GROUP}
ENVEOF
      chmod 600 "$CANONICAL_ENV"
      # Symlink into repo (gitignored)
      ln -sf "$CANONICAL_ENV" "$REPO_ENV"
    fi
    _ok
  fi
fi

# ── Step 4: Python venv + dependencies ───────────────────────────────────────
_step "Python venv + dependencies"
VENV_DIR="$REPO_DIR/venv"
REQ="$REPO_DIR/requirements.txt"

if [[ "$CURRENT_IP" == "$VPS_IP" ]]; then
  _skip "VPS — Python deps handled by Docker image"
elif [[ ! -f "$REQ" ]]; then
  _warn "requirements.txt not found at $REQ"
else
  if [[ -d "$VENV_DIR" && $RESET -eq 0 ]]; then
    _skip "venv already exists at $VENV_DIR"
  elif [[ $CHECK_ONLY -eq 0 ]]; then
    python3 -m venv "$VENV_DIR" && \
      "$VENV_DIR/bin/pip" install --quiet -r "$REQ" && \
      _ok || _fail "venv or pip install failed"
  else
    _ok  # check-only: requirements.txt present
  fi
fi

# ── Step 4.5: Enable pre-commit hook ──────────────────────────────────────────
_step "Pre-commit leak-scrub hook (core.hooksPath = .githooks)"
if [[ $CHECK_ONLY -eq 1 ]]; then
  if [[ "$(git -C "$REPO_DIR" config --get core.hooksPath 2>/dev/null)" == ".githooks" ]]; then _ok; else _fail "core.hooksPath not set to .githooks"; fi
else
  git -C "$REPO_DIR" config core.hooksPath .githooks
  _ok
fi

# ── Step 5: SSH executor key ──────────────────────────────────────────────────
_step "SSH executor key (Mac → VPS)"
EXECUTOR_KEY="$HOME/.ssh/aaka_executor"
SSH_CONFIG="$HOME/.ssh/config"
SSH_ALIAS="${AAKA_VPS_HOST:-aaka-away}"

if [[ -n "$VPS_IP" && "$CURRENT_IP" == "$VPS_IP" ]]; then
  _skip "VPS — SSH key not needed here"
elif [[ -f "$EXECUTOR_KEY" && $RESET -eq 0 ]]; then
  _skip "key already exists at $EXECUTOR_KEY"
elif [[ $CHECK_ONLY -eq 0 ]]; then
  ssh-keygen -t ed25519 -f "$EXECUTOR_KEY" -N "" -C "aaka-executor" -q
  # Add SSH config block (idempotent). Host alias is configurable via
  # AAKA_VPS_HOST; HostName comes from the .vps_host field in the JSON config.
  if ! grep -q "Host ${SSH_ALIAS}" "$SSH_CONFIG" 2>/dev/null; then
    mkdir -p "$HOME/.ssh" && touch "$SSH_CONFIG" && chmod 600 "$SSH_CONFIG"
    [[ -s "$SSH_CONFIG" ]] && echo "" >> "$SSH_CONFIG"
    VPS_IP_CFG="$(_jq '.vps_host' '<your-vps-ip>')"
    cat >> "$SSH_CONFIG" <<SSHBLOCK
Host ${SSH_ALIAS}
    HostName ${VPS_IP_CFG}
    User root
    IdentityFile ~/.ssh/aaka_executor
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    AddKeysToAgent yes
    UseKeychain yes
SSHBLOCK
  fi
  _ok
  echo ""
  echo "  Mac executor pubkey (authorize this on VPS during Phase 3):"
  echo "  ────────────────────────────────────────────────────────────"
  cat "${EXECUTOR_KEY}.pub" | sed 's/^/  /'
  echo "  ────────────────────────────────────────────────────────────"
  echo ""
else
  # check-only
  if [[ -f "$EXECUTOR_KEY" ]]; then _ok; else _fail "key not found: $EXECUTOR_KEY"; fi
fi

# ── Step 6: VPS reachability ──────────────────────────────────────────────────
_step "VPS SSH reachability"
VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"

if [[ -n "$VPS_IP" && "$CURRENT_IP" == "$VPS_IP" ]]; then
  _skip "already on VPS"
elif ssh -o BatchMode=yes -o ConnectTimeout=5 "${VPS_HOST}" "echo ok" &>/dev/null 2>&1; then
  _ok
else
  _warn "VPS not reachable yet — authorize the executor pubkey (see Step 5 output) then re-run"
fi

# ── Step 7: credentials.json ──────────────────────────────────────────────────
_step "Google credentials.json"
CREDS="$AAKA_CONFIG_DIR/tokens/credentials.json"

if [[ -f "$CREDS" ]]; then
  _ok
else
  _fail "not found at $CREDS — download from GCP Console (see INSTALL.md Phase 2)"
fi

# ── Step 8: Config validity (python import) ───────────────────────────────────
_step "Config loads cleanly"
if [[ -f "$AAKA_CONFIG_DIR/config/aaka.yaml" ]]; then
  result=$(AAKA_CONFIG_DIR="$AAKA_CONFIG_DIR" AAKA_BASE="$REPO_DIR" \
    python3 -c "
import sys; sys.path.insert(0, '${REPO_DIR}')
import aaka_config
members = aaka_config.carriers()
print(f'OK: {len(members)} member(s)')
" 2>&1)
  if echo "$result" | grep -q "^OK:"; then
    _ok "$(echo "$result" | grep '^OK:')"
  else
    _warn "config loaded but check failed: $(echo "$result" | tail -1)"
  fi
else
  _warn "aaka.yaml not yet present — complete Phase 1 first"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
if [[ $FAIL_COUNT -eq 0 ]]; then
  echo "Setup complete — no failures. Run: bash admin/aaka.sh diagnose"
else
  echo "Setup finished with ${FAIL_COUNT} failure(s). Fix [FAIL] lines above and re-run."
  exit 1
fi
