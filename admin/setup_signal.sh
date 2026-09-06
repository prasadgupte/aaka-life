#!/usr/bin/env bash
# admin/setup_signal.sh — connect aaka to Signal, end to end.
#
# Signal has no bot platform: no BotFather, no bot accounts, no official API.
# aaka therefore drives a REAL Signal account through signal-cli. That leaves
# exactly two models, and picking the wrong one is the mistake this script
# exists to prevent:
#
#   link      aaka becomes a second device on YOUR Signal account. Nothing to
#             register, no spare number. But aaka then sees every conversation
#             you have and replies AS you. Safe for your phone; not a separate
#             identity.
#
#   register  aaka gets its OWN number and its own contact card, exactly like a
#             Telegram bot. Needs a spare number that can take one SMS or call.
#             WARNING: registering a number that is currently active on a phone
#             DEREGISTERS Signal on that phone. There is no undo.
#
# Run:  bash admin/setup_signal.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$HERE")"
# shellcheck source=/dev/null
[ -f "$HERE/lib/common.sh" ] && . "$HERE/lib/common.sh" || {
  ok(){ echo "  ✓ $*"; }; fail(){ echo "  ✗ $*" >&2; }; warn(){ echo "  ! $*"; }
  info(){ echo "  ▸ $*"; }; header(){ echo; echo "$*"; }; }

ENV_FILE="${AAKA_ENV_FILE:-$REPO_DIR/.env}"
PY="$REPO_DIR/venv/bin/python3"; [ -x "$PY" ] || PY="$(command -v python3)"

# Set KEY=VALUE in .env, replacing any existing line.
set_env() {
  local k="$1" v="$2"
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  if grep -qE "^${k}=" "$ENV_FILE" 2>/dev/null; then
    local tmp; tmp="$(mktemp)"
    grep -vE "^${k}=" "$ENV_FILE" > "$tmp"; mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  fi
  printf '%s=%s\n' "$k" "$v" >> "$ENV_FILE"
  ok "$k set in $(basename "$ENV_FILE")"
}

# Fallback QR renderer. The link path uses admin/signal_pair.py (a browser page
# that refreshes the code and shows live status); this stays for headless hosts
# where no browser can be opened — call it with a URI to print a scannable code.
show_qr() {
  local uri="$1" png="${2:-/tmp/aaka-signal-link.png}"
  "$PY" - "$uri" "$png" <<'PYQR' 2>/dev/null || { echo; echo "  Link URI (paste into a QR generator):"; echo "  $uri"; return 0; }
import sys
try:
    import qrcode
except ImportError:
    raise SystemExit(1)
uri, png = sys.argv[1], sys.argv[2]
qrcode.make(uri).resize((600, 600)).save(png)
q = qrcode.QRCode(border=1); q.add_data(uri); q.make(); q.print_ascii(invert=True)
print(f"\n  Also saved as an image: {png}")
PYQR
}

header "aaka — Signal setup"

if ! command -v signal-cli &>/dev/null; then
  warn "signal-cli is not installed."
  info "Run 'bash admin/deploy.sh' first — it installs signal-cli and the JRE"
  info "on both the Mac and the VPS. Then re-run this script."
  exit 1
fi
ok "signal-cli: $(signal-cli --version 2>/dev/null | head -1)"

MODE="${1:-}"
if [ -z "$MODE" ]; then
  echo
  echo "  How should aaka connect to Signal?"
  echo
  echo "    1) link      — aaka joins YOUR Signal account as a second device."
  echo "                   No spare number needed. aaka sees all your Signal"
  echo "                   conversations and replies as you."
  echo "    2) register  — aaka gets its OWN number and contact card."
  echo "                   Needs a spare number. NEVER use a number that is"
  echo "                   already active on a phone: it would lose Signal."
  echo
  read -rp "  Choose [1/2]: " _c
  case "$_c" in 1) MODE=link ;; 2) MODE=register ;; *) fail "Pick 1 or 2."; exit 1 ;; esac
fi

case "$MODE" in
link)
  header "Linking aaka to your Signal account"
  # Browser page rather than a terminal QR: Signal's link code expires after a
  # couple of minutes, and a page that mints a fresh one (and shows live status)
  # removes the race the terminal flow puts on the user. Same shape as the
  # WhatsApp sidecar's pairing page.
  PAIR_PORT="${SIGNAL_PAIR_PORT:-18795}"
  info "Opening the pairing page at http://127.0.0.1:$PAIR_PORT/ …"
  info "Scan it from Signal → Settings → Linked Devices → +"
  echo
  if "$PY" "$HERE/signal_pair.py" --port "$PAIR_PORT" --name aaka; then
    ACCOUNT="$(signal-cli listAccounts 2>/dev/null | grep -oE '\+[0-9]+' | head -1 || true)"
    [ -n "$ACCOUNT" ] && ok "linked to $ACCOUNT" || warn "linked, but could not read the account number"
  else
    fail "Linking did not complete."
    exit 1
  fi
  # Linked mode is a safety switch, not a preference: aaka shares the operator's
  # account, so it must stay silent on anything that is not an explicit command.
  set_env SIGNAL_LINKED_MODE "true"
  info "SIGNAL_LINKED_MODE=true — aaka will answer /commands and shortcuts only,"
  info "and stay silent in your personal conversations."
  ;;
register)
  header "Registering a dedicated number for aaka"
  read -rp "  Number in E.164 (e.g. +15550000000): " ACCOUNT
  [ -n "$ACCOUNT" ] || { fail "No number given."; exit 1; }
  echo
  warn "If $ACCOUNT is currently used by Signal on a phone, that phone LOSES"
  warn "Signal when this completes. There is no undo."
  read -rp "  Type YES to continue: " _y
  [ "$_y" = "YES" ] || { info "Aborted."; exit 1; }
  read -rp "  Deliver the code by voice call instead of SMS? [y/N]: " _v
  VOICE=""; [[ "$_v" =~ ^[Yy] ]] && VOICE="--voice"
  if ! signal-cli -a "$ACCOUNT" register $VOICE; then
    echo
    warn "Registration was rejected — Signal almost always wants a captcha."
    info "Open this in a browser, solve it, then right-click 'Open Signal' and"
    info "copy the link (it starts with signalcaptcha://):"
    info "  https://signalcaptchas.org/registration/generate.html"
    read -rp "  Paste the signalcaptcha:// token: " CAPTCHA
    [ -n "$CAPTCHA" ] || { fail "No token given."; exit 1; }
    signal-cli -a "$ACCOUNT" register $VOICE --captcha "$CAPTCHA" \
      || { fail "Registration still failed."; exit 1; }
  fi
  read -rp "  Enter the verification code you received: " CODE
  signal-cli -a "$ACCOUNT" verify "$CODE" || { fail "Verification failed."; exit 1; }
  ok "registered $ACCOUNT"
  set_env SIGNAL_LINKED_MODE "false"
  ;;
*) fail "Unknown mode '$MODE' (use: link | register)"; exit 1 ;;
esac

[ -n "${ACCOUNT:-}" ] && set_env SIGNAL_ACCOUNT "$ACCOUNT"

# Add signal to ENABLED_CHANNELS without dropping what is already there.
CUR="$(grep -E '^ENABLED_CHANNELS=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\042\047 ' || true)"
CUR="${CUR:-telegram}"
echo "$CUR" | grep -q signal || set_env ENABLED_CHANNELS "$CUR,signal"

header "Next"
info "1. bash admin/deploy.sh     — installs the daemon + poller services"
info "2. bash admin/diagnose.sh   — read the Signal section"
info "3. Message aaka on Signal and send:  /menu"
echo
