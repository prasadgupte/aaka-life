#!/usr/bin/env python3
"""Daily check: warn via Telegram if LinkedIn token expires within 14 days."""

import json
import os
import sys
import time
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(BASE))
import aaka_config

TOKEN_FILE = aaka_config.secrets_root() / "linkedin-tool" / "tokens.json"
LINKEDIN_TOOL_DIR = os.environ.get("AAKA_LINKEDIN_TOOL", "")
WARN_DAYS = 14


def main():
    if not TOKEN_FILE.exists():
        print("No LinkedIn token file found.")
        return

    t = json.loads(TOKEN_FILE.read_text())
    expiry = t.get("saved_at", 0) + t.get("expires_in", 0)
    days_left = int((expiry - time.time()) / 86400)
    expiry_date = time.strftime("%Y-%m-%d", time.localtime(expiry))

    print(f"LinkedIn token expires: {expiry_date} ({days_left} days)")

    if days_left > WARN_DAYS:
        return

    # Send Telegram warning
    from message_send import send
    auth_hint = (
        f"`python3 {LINKEDIN_TOOL_DIR}/auth.py`"
        if LINKEDIN_TOOL_DIR
        else "the linkedin-tool auth script (set AAKA_LINKEDIN_TOOL)"
    )
    msg = (
        f"⚠️ *LinkedIn token expires in {days_left} day(s)* ({expiry_date}).\n\n"
        f"Re-authorize by running:\n"
        f"{auth_hint}\n"
        f"or visit the OAuth URL directly."
    )
    send(msg)
    print(f"Warning sent via Telegram ({days_left} days remaining).")


if __name__ == "__main__":
    main()
