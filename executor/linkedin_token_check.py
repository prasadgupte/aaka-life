#!/usr/bin/env python3
"""Daily check: warn via Telegram if LinkedIn token expires within 14 days."""

import json
import os
import sys
import time
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(BASE))

TOKEN_FILE = Path("/Users/Shared/secrets/linkedin-tool/tokens.json")
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
    msg = (
        f"⚠️ *LinkedIn token expires in {days_left} day(s)* ({expiry_date}).\n\n"
        f"Re-authorize by running:\n"
        f"`python3 /Users/Shared/tools/linkedin-tool/auth.py`\n"
        f"or visit the OAuth URL directly."
    )
    send(msg)
    print(f"Warning sent via Telegram ({days_left} days remaining).")


if __name__ == "__main__":
    main()
