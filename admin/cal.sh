#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
AAKA_BASE="${AAKA_BASE:-$REPO_DIR}"
AAKA_CONFIG_DIR="${AAKA_CONFIG_DIR:-$HOME/.aaka}"

AAKA_BASE="$AAKA_BASE" AAKA_CONFIG_DIR="$AAKA_CONFIG_DIR" \
    "$REPO_DIR/venv/bin/python3" "$REPO_DIR/skills/calendar/sidecar_sync.py" --check
