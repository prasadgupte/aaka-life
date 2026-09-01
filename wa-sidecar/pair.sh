#!/bin/bash
# Interactive WhatsApp pairing for the aaka sidecar (pairing-code flow).
#
#   bash wa-sidecar/pair.sh <number-digits>      e.g. bash wa-sidecar/pair.sh 4915123146203
#
# Prints an 8-char code to type into WhatsApp → Settings → Linked Devices →
# Link a device → Link with phone number. If WhatsApp is rate-limiting this
# network, it says so — re-run over a different network (phone hotspot).
set -u
NUM="$(echo "${1:-}" | tr -cd '0-9')"
[ -z "$NUM" ] && { echo "usage: bash wa-sidecar/pair.sh <number-digits, e.g. 4915123146203>"; exit 2; }

REPO="/Users/Shared/aaka-repo"
CFG="${AAKA_CONFIG_DIR:-/Users/Shared/aaka-repo-config}"
LOG="$CFG/logs/wa_sidecar.log"

# 1. inbound receiver (so a paired number's messages route immediately)
if ! curl -s -m3 http://127.0.0.1:18793/health >/dev/null 2>&1; then
  echo "· starting inbound receiver (:18793)…"
  ( cd "$REPO" && ENABLED_CHANNELS=telegram,whatsapp AAKA_CONFIG_DIR="$CFG" \
      venv/bin/python3 -m uvicorn sensor.wa_inbound:app --host 127.0.0.1 --port 18793 \
      >"$CFG/logs/wa_receiver.log" 2>&1 & )
  sleep 3
fi

# 2. fresh sidecar in pairing-code mode
lsof -ti :18792 2>/dev/null | xargs -r kill 2>/dev/null
echo "· starting sidecar (pairing-code mode) for ${NUM:0:4}****${NUM: -2}…"
( cd "$REPO/wa-sidecar" && WA_SIDECAR_PORT=18792 AAKA_CONFIG_DIR="$CFG" \
    WA_PAIRING_NUMBER="$NUM" nohup node index.js >"$LOG" 2>&1 & )

# 3. poll for a code (or a rate-limit verdict)
printf "· waiting for pairing code"
CODE=""; ST=""
for _ in $(seq 1 20); do
  sleep 2; printf "."
  R="$(curl -s -m3 http://127.0.0.1:18792/pair 2>/dev/null)"
  CODE="$(printf '%s' "$R" | grep -o '"code":"[^"]*"' | cut -d'"' -f4)"
  ST="$(printf '%s'  "$R" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)"
  [ -n "$CODE" ] && break
  [ "$ST" = "rate_limited" ] && break
done
echo ""

if [ -n "$CODE" ]; then
  echo ""
  echo "   ╔══════════════════════════════════╗"
  echo "   ║   WhatsApp pairing code:  $CODE"
  echo "   ╚══════════════════════════════════╝"
  echo "   Phone → WhatsApp → Settings → Linked Devices →"
  echo "   Link a device → Link with phone number → enter the code."
  echo "   (Sidecar stays running and finishes the link once you enter it.)"
  echo "   Verify:  curl -s localhost:18792/status"
elif [ "$ST" = "rate_limited" ]; then
  echo "⚠  WhatsApp is rate-limiting pairing from this network."
  curl -s -m3 http://127.0.0.1:18792/status | grep -o '"message":"[^"]*"' | cut -d'"' -f4
  echo "→  Re-run this over a different network (phone hotspot), or wait a few hours."
  lsof -ti :18792 2>/dev/null | xargs -r kill 2>/dev/null
else
  echo "No code yet and not explicitly rate-limited. Tail the log:"
  echo "   tail -20 $LOG"
fi
