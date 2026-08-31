"""
tools/load_env.py — dependency-free .env loader for native (non-Docker) runs.

The Docker path gets env from docker-compose; run natively, nothing sources .env.
load_env() reads <repo>/.env and fills any keys that aren't already set, so:
  - native runs pick up TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, etc.
  - container/explicit env always wins (we never overwrite an existing key).
No-op if .env is absent.
"""
import os
from pathlib import Path


def load_env(base: "str | Path | None" = None) -> None:
    base = Path(base or os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
    env_file = base / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception:
        pass  # never let a malformed .env crash startup
