#!/usr/bin/env python3
"""
sensor/telegram_multibot.py — multi-bot Telegram supervisor.

Runs one `telegram_poller` process per configured bot, each with its own
TELEGRAM_BOT_TOKEN + AAKA_BOT_ID (which keys the offset file and tags inbound
metadata so replies route back via the same bot). Crashed pollers are restarted.

Bots come from $AAKA_CONFIG_DIR/tokens/telegram_bots.json ({"<id>": "<token>"}).
With no such file, it runs the single default bot (AAKA_BOT_ID="") — fully
back-compatible with the existing single-poller setup.

Run: python3 sensor/telegram_multibot.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Default AAKA_BASE to this repo (native run); the container sets it to /app explicitly.
BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parents[1])
os.environ.setdefault("AAKA_BASE", str(BASE))  # so spawned pollers inherit it
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
POLLER = BASE / "sensor" / "telegram_poller.py"


def _default_token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if tok:
        return tok
    creds = CONFIG_DIR / "tokens" / "message_send.json"
    if creds.exists():
        try:
            return json.loads(creds.read_text()).get("bot_token", "")
        except Exception:
            pass
    return ""


def resolve_bots() -> "list[tuple[str, str]]":
    """Return [(bot_id, token), ...]. Pure — safe to unit-test.

    Multi-bot (tokens/telegram_bots.json present + non-empty): one entry per bot.
    Otherwise: a single default bot with id "" (back-compat). Empty if no token.
    """
    path = CONFIG_DIR / "tokens" / "telegram_bots.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            bots = [(str(k), str(v)) for k, v in data.items() if v]
            if bots:
                return bots
        except Exception:
            pass
    default = _default_token()
    return [("", default)] if default else []


def _spawn(bot_id: str, token: str) -> subprocess.Popen:
    env = {**os.environ, "TELEGRAM_BOT_TOKEN": token, "AAKA_BOT_ID": bot_id}
    label = bot_id or "default"
    print(f"[multibot] starting poller for bot '{label}'", flush=True)
    return subprocess.Popen([sys.executable, str(POLLER)], env=env)


def main() -> None:
    # Native runs: pull TELEGRAM_BOT_TOKEN etc. from .env (Docker gets it from compose).
    try:
        from tools.load_env import load_env
        load_env()
    except Exception:
        pass
    bots = resolve_bots()
    if not bots:
        print("[multibot] no bots configured (no token) — exiting", file=sys.stderr)
        sys.exit(1)

    procs: "dict[str, subprocess.Popen]" = {}
    for bot_id, token in bots:
        procs[bot_id] = _spawn(bot_id, token)
    tokens = dict(bots)

    # Supervise: restart any poller that dies (simple backoff).
    while True:
        time.sleep(3)
        for bot_id, p in list(procs.items()):
            if p.poll() is not None:
                print(f"[multibot] poller '{bot_id or 'default'}' exited "
                      f"({p.returncode}); restarting", file=sys.stderr)
                time.sleep(2)
                procs[bot_id] = _spawn(bot_id, tokens[bot_id])


if __name__ == "__main__":
    main()
