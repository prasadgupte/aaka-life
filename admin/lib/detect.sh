#!/usr/bin/env bash
# admin/lib/detect.sh — Instance + mode detection
#
# VPS_IP and AAKA_VPS_HOST are read from $AAKA_CONFIG_DIR/.env if present,
# else from the environment. Set AAKA_VPS_IP to your VPS's public IP and
# AAKA_VPS_HOST to your SSH alias for the VPS (default: aaka-away).

# Establish config dir (env var wins; default is ~/.aaka for new installations)
AAKA_CONFIG_DIR="${AAKA_CONFIG_DIR:-$HOME/.aaka}"

_VPS_ENV="$AAKA_CONFIG_DIR/.env"
if [ -f "$_VPS_ENV" ]; then
    # shellcheck disable=SC1090
    set -a; . "$_VPS_ENV"; set +a
fi

VPS_IP="${AAKA_VPS_IP:-}"
AAKA_VPS_HOST="${AAKA_VPS_HOST:-aaka-away}"
CURRENT_IP=$(curl -s --max-time 3 ifconfig.me 2>/dev/null || echo "unknown")

if [ -n "$VPS_IP" ] && [ "$CURRENT_IP" = "$VPS_IP" ]; then
    INSTANCE="vps"
    MODE="&away"
    REPO_DIR="/opt/aaka"
    AAKA_CONFIG_DIR="/opt/aaka-config"
else
    INSTANCE="local"
    MODE="&home"
    # Resolve repo root relative to this script — works wherever the repo is cloned
    REPO_DIR="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null \
        || dirname "$(dirname "${BASH_SOURCE[0]}")")"
    # AAKA_CONFIG_DIR already set above; preserve it
fi

AAKA_BASE="${REPO_DIR}"
CONTEXT="${AAKA_CONTEXT:-family}"
