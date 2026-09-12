#!/usr/bin/env python3
"""
sensor/test_start_welcome.py — a roster member pressing Telegram's Start button
gets the welcome (not "Sorry, I didn't understand that").

Telegram sends `/start` when a user opens a bot for the first time, and
`/start <code>` from an invite link. Members already in aaka.yaml never went
through the invite path, so their first message used to hit the fallback.
Run: python3 sensor/test_start_welcome.py   (exit 0 = pass)
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ["AAKA_CONFIG_DIR"] = str(REPO_ROOT / "samples" / "demo")
os.environ["SIGNAL_LINKED_MODE"] = "false"

import aaka_config  # noqa: E402
import sensor.router_sensor as rs  # noqa: E402

_FAILURES = []


def check(desc, cond):
    print(("  ok   " if cond else "  FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


def _envelope(sender: str, text: str) -> str:
    meta = {"chat_id": f"telegram:{sender}", "message_id": "1",
            "sender_id": sender, "conversation_label": f"id:{sender}"}
    return "Conversation info (untrusted metadata):\n```json\n" + json.dumps(meta) + "\n```\n" + text


def main():
    aaka_config._load.cache_clear()
    member = next(m for m in aaka_config.members() if m.get("telegram") or m.get("telegram_id"))
    tg = str(member.get("telegram") or member.get("telegram_id"))
    rs._react_read = lambda *a, **k: None   # no network

    out = rs.route(_envelope(tg, "/start"))
    check("/start from a roster member is a welcome", "you're all set" in out)
    check("welcome names them", (member.get("nick") or member.get("name")) in out)
    check("welcome is not the fallback", "didn't understand" not in out)

    out = rs.route(_envelope(tg, "/start abc123"))
    check("/start <code> from a roster member is the same welcome", "you're all set" in out)

    out = rs.route(_envelope(tg, "/starting a diet t"))
    check("'/starting…' is not treated as /start", "you're all set" not in out)

    stranger = "999000111"
    out = rs.route(_envelope(stranger, "/start"))
    check("/start from a stranger still goes through onboarding, not the welcome",
          "you're all set" not in out)

    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all /start welcome checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
