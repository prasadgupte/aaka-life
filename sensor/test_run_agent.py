#!/usr/bin/env python3
"""
sensor/test_run_agent.py — `r` / `/run`: the alias, fire vs hold, and the bare-yes rule.

A held agent prompt (`r <agent>? …`) must only fire from its own buttons or an
explicit `yes #<id8>`. The bare-yes binder ("bind to the only open preview")
would otherwise turn a stray "yes" in any chat into a Claude run on the home
machine, so it skips agent_dispatch items.
Run: python3 sensor/test_run_agent.py   (exit 0 = pass)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ["AAKA_CONFIG_DIR"] = str(REPO_ROOT / "samples" / "demo")
_TMP = tempfile.mkdtemp(prefix="aaka-run-")
os.environ["QUEUE_DB"] = str(Path(_TMP) / "butler.db")
os.environ["SIGNAL_LINKED_MODE"] = "false"
os.environ["AAKA_ROLE"] = "sensor"

import aaka_config  # noqa: E402
from aaka_queue import queue as q  # noqa: E402
import gateway.dispatch as dispatch  # noqa: E402
import sensor.router_sensor as rs  # noqa: E402

_FAILURES = []


def check(desc, cond):
    print(("  ok   " if cond else "  FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


def _envelope(sender: str, text: str, message_id="77") -> str:
    meta = {"chat_id": f"telegram:{sender}", "message_id": str(message_id),
            "sender_id": sender, "conversation_label": f"id:{sender}"}
    return "Conversation info (untrusted metadata):\n```json\n" + json.dumps(meta) + "\n```\n" + text


def _agent_items():
    with q._connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, status, payload FROM queue_items WHERE intent='agent_dispatch' "
            "ORDER BY created_at, rowid").fetchall()]


def main():
    aaka_config._load.cache_clear()
    admin = next(m for m in aaka_config.members() if m.get("admin") and m.get("telegram"))
    tg = str(admin["telegram"])
    rs._react_read = lambda *a, **k: None
    # A registry without touching the demo config dir.
    specs = {"fa": dispatch._spec({"dir": _TMP, "desc": "paperwork"}),
             "ta": dispatch._spec({"dir": _TMP, "mode": "hold"})}
    dispatch.agent_specs = lambda: dict(specs)

    out = rs.route(_envelope(tg, "r"))
    check("bare `r` from an admin lists the agents", "Agents" in out and "`fa`" in out)

    out = rs.route(_envelope(tg, "r fa! what is due this week"))
    items = _agent_items()
    check("`r fa! …` queues one item as 'confirmed'",
          len(items) == 1 and items[0]["status"] == "confirmed")
    check("the prompt is stored without the switch",
          json.loads(items[0]["payload"])["request"] == "what is due this week")

    out = rs.route(_envelope(tg, "r ta draft the reply"))
    held = _agent_items()[-1]
    check("an agent with mode: hold parks the prompt as awaiting_confirm",
          held["status"] == "awaiting_confirm")
    check("the held reply carries Fire/Drop buttons", "Fire" in out and "__MARKUP__" in out)

    rs.route(_envelope(tg, "yes", message_id="78"))
    check("a bare 'yes' does NOT fire a held agent prompt",
          q.get_item(held["id"])["status"] == "awaiting_confirm")

    rs.route(_envelope(tg, f"yes #{held['id'][:8]}", message_id="79"))
    check("`yes #<id8>` fires it", q.get_item(held["id"])["status"] == "confirmed")

    if _FAILURES:
        print(f"\n{len(_FAILURES)} failure(s)")
        sys.exit(1)
    print("\nall ok")


if __name__ == "__main__":
    main()
