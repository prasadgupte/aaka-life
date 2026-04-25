#!/usr/bin/env bash
# admin/demo.sh — Launch the Aaka demo REPL (Ash-Kaa family, no Docker, no API keys needed)
#
# Usage:
#   bash admin/demo.sh          # start REPL (preserves state from last session)
#   bash admin/demo.sh --reset  # restore seed data first, then start REPL

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DEMO_DIR="$REPO_DIR/samples/demo"

# ── Reset seed data if requested ────────────────────────────────────────────
if [[ "${1:-}" == "--reset" ]]; then
    echo "  Restoring demo seed data..."
    git -C "$REPO_DIR" checkout -- samples/demo/data/lists/ samples/demo/data/notes/ 2>/dev/null || true
    rm -f "$DEMO_DIR/data/queue/butler.db" 2>/dev/null || true
    echo "  Done."
fi

# ── Runtime dirs (queue DB, logs) ────────────────────────────────────────────
mkdir -p "$DEMO_DIR/data/queue" "$DEMO_DIR/logs"

# ── Python: prefer venv, fall back to system ─────────────────────────────────
if [ -f "$REPO_DIR/venv/bin/python3" ]; then
    PYTHON="$REPO_DIR/venv/bin/python3"
else
    PYTHON="python3"
fi

# ── Dep check: pyyaml (minimum requirement) ──────────────────────────────────
if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
    echo "  Installing pyyaml..."
    "$PYTHON" -m pip install --quiet --user pyyaml
fi

# ── Launch REPL ──────────────────────────────────────────────────────────────
exec env \
    AAKA_BASE="$REPO_DIR" \
    AAKA_CONFIG_DIR="$DEMO_DIR" \
    QUEUE_DB="$DEMO_DIR/data/queue/butler.db" \
    "$PYTHON" "$SCRIPT_DIR/demo_repl.py" "$@"
