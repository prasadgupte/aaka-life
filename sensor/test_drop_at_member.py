#!/usr/bin/env python3
"""`@` is reserved for members in /drop and /note.

  f @alice #health   → alice's vault (same as `f alice #health`, same as `t @alice`)
  f @nobody #health  → never silently the sender: the household vault, ack says
                       "everyone?" and lists the members so it is one /undo away
  f #health          → the sender's own vault (unchanged)

Runs the real router in dry-run against a throwaway config dir.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CFG = Path(tempfile.mkdtemp(prefix="aaka-drop-at-"))
os.environ["AAKA_CONFIG_DIR"] = str(CFG)
(CFG / "config").mkdir()
(CFG / "data" / "staging").mkdir(parents=True)
(CFG / "config" / "aaka.yaml").write_text(
    "timezone: Europe/Berlin\n"
    "members:\n"
    "  - {id: alice, name: Alice, telegram_id: '111', role: admin, admin: true, namespace: alice}\n"
    "  - {id: bob, name: Bob, telegram_id: '222', role: member, namespace: bob}\n"
    "  - {id: home, name: Home, role: family, namespace: home}\n"
    "  - {id: bot, name: Bot, role: bot}\n"
)
PDF = CFG / "data" / "staging" / "scan.pdf"
PDF.write_bytes(b"%PDF-1.4 test")

import aaka_config  # noqa: E402
from sensor import router_sensor as rs  # noqa: E402

FAILS = 0


def check(desc, cond):
    global FAILS
    print(f"  {'ok  ' if cond else 'FAIL'} {desc}")
    if not cond:
        FAILS += 1


def envelope(text, sender="111"):
    return ("Conversation info (untrusted metadata):\n```json\n"
            f'{{"chat_id": "telegram:{sender}", "message_id": "1", "sender_id": "{sender}"}}\n```\n'
            f"[media attached: {PDF} (application/pdf)]\n{text}\n")


def run(text, sender="111"):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply = rs.route(envelope(text, sender), dry_run=True) or ""
    out = buf.getvalue()
    payload = json.loads(out.split("payload=", 1)[1]) if "payload=" in out else {}
    return reply, payload


def main():
    check("member_by_name strips a leading @", aaka_config.member_by_name("@Bob")["id"] == "bob")
    check("people_names hides bot + household", aaka_config.people_names() == ["Alice", "Bob"])
    check("group_member_id is the role: family member", aaka_config.group_member_id() == "home")

    reply, p = run("f @bob #health")
    check("f @bob → bob's vault", p.get("actor") == "bob" and p.get("namespace") == "bob")
    check("  …ack names bob, no guess marker", "bob/" in reply and "❓" not in reply)

    reply, p = run("f bob #health")
    check("f bob (bare) still works", p.get("actor") == "bob")

    reply, p = run("f @nobody #health")
    check("f @nobody → household vault, not the sender", p.get("actor") == "home" and p.get("namespace") == "home")
    check("  …ack says everyone? and lists members",
          "everyone?/" in reply and "❓" in reply and "Alice, Bob" in reply and "@nobody" in reply)

    reply, p = run("f #health")
    check("f #health (no member) → sender's own vault", p.get("actor") is None and p.get("namespace") == "alice")

    reply, p = run("f @nobody #health", sender="222")
    check("non-admin @nobody → own vault, marked as a guess", p.get("actor") == "bob" and "you?/" in reply and "❓" in reply)

    reply, p = run("n @nobody health saw the doc")
    check("n @nobody → household note with the guess marker", "❓" in reply and "@nobody" in reply and "#health" in reply)

    shutil.rmtree(CFG, ignore_errors=True)
    print("\nall drop @member checks passed" if not FAILS else f"\n{FAILS} FAILED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
