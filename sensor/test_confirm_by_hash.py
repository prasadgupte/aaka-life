#!/usr/bin/env python3
"""
sensor/test_confirm_by_hash.py — two previews in flight, both confirmable.

2026-09-21: two /cal previews arrived (the home hand-off sent both), the user
tapped Yes on each; the first landed, the second got "Sorry, I didn't
understand that". Cause: one pending_confirm slot per sender — the second
preview took the slot (and cancelled the first item), the first tap consumed
it, the second tap had nothing to bind to. Now the buttons carry the item
hash (`yes #abc12345`), a bare yes/cancel with no slot binds to the only open
preview or asks which one, and the fallback names the message it answers.
Run: python3 sensor/test_confirm_by_hash.py   (exit 0 = pass)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ["AAKA_CONFIG_DIR"] = str(REPO_ROOT / "samples" / "demo")
_TMP = tempfile.mkdtemp(prefix="aaka-confirm-")
os.environ["QUEUE_DB"] = str(Path(_TMP) / "butler.db")
os.environ["SIGNAL_LINKED_MODE"] = "false"
os.environ["AAKA_ROLE"] = "sensor"

import aaka_config  # noqa: E402
from aaka_queue import queue as q  # noqa: E402
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


def _event_item(sender, title):
    iid = q.write_item(intent="add_event", raw_message=f"/cal {title}", sender=sender,
                       channel_id=sender, source="telegram",
                       payload={"title": title, "summary": title, "attendee": "Alex",
                                "occurrences": [], "type": "other"})
    q.update_status(iid, "awaiting_confirm")
    return iid


def main():
    aaka_config._load.cache_clear()
    member = next(m for m in aaka_config.members() if m.get("telegram") or m.get("telegram_id"))
    tg = str(member.get("telegram") or member.get("telegram_id"))
    rs._react_read = lambda *a, **k: None


    # ── confirm_markup carries the hash ────────────────────────────────────
    mk = q.confirm_markup("abcdef1234567890", "Post ✅")
    btns = mk["inline_keyboard"][0]
    check("markup: yes button data = 'yes #<id8>'", btns[0]["callback_data"] == "yes #abcdef12")
    check("markup: cancel button data = 'cancel #<id8>'", btns[1]["callback_data"] == "cancel #abcdef12")
    check("markup: labels are configurable", btns[0]["text"] == "Post ✅" and btns[1]["text"] == "Cancel ❌")

    # ── two previews in flight: the slot moves, the first item survives ────
    a = _event_item(tg, "Dentist")
    q.set_pending_confirm(tg, a)
    b = _event_item(tg, "Dinner")
    q.set_pending_confirm(tg, b)
    check("second preview does NOT cancel the first item any more",
          q.get_item(a)["status"] == "awaiting_confirm")
    check("slot points at the latest preview", q.get_pending_confirm(tg)["item_id"] == b)

    # tap Yes on the LATEST (slot) → normal path
    out = rs.route(_envelope(tg, f"yes #{b[:8]}"))
    check("yes #<latest> confirms it", q.get_item(b)["status"] in ("confirmed", "done") and "❌" not in out)
    # tap Yes on the FIRST — slot is gone now; the hash still binds
    out = rs.route(_envelope(tg, f"yes #{a[:8]}"))
    check("yes #<first> after the slot was consumed still confirms it",
          q.get_item(a)["status"] in ("confirmed", "done"))
    check("…and does not answer 'didn't understand'", "didn't understand" not in out)

    # ── addressed cancel + unknown hash ────────────────────────────────────
    c = _event_item(tg, "Physio")
    out = rs.route(_envelope(tg, f"cancel #{c[:8]}"))
    check("cancel #<id> cancels that item", q.get_item(c)["status"] == "cancelled")
    out = rs.route(_envelope(tg, "yes #deadbeef"))
    check("yes #<unknown> explains instead of 'didn't understand'",
          "Nothing of yours is waiting" in out and "deadbeef" in out)
    # another sender's item is not reachable by hash
    d = _event_item("999000111", "Not yours")
    out = rs.route(_envelope(tg, f"yes #{d[:8]}"))
    check("a hash belonging to someone else is not confirmable", q.get_item(d)["status"] == "awaiting_confirm")

    # ── bare yes with no slot ──────────────────────────────────────────────
    q.clear_pending_confirm(tg)
    out = rs.route(_envelope(tg, "yes", message_id="2757"))
    check("bare yes, nothing open → says so, with the message ref",
          "Nothing is waiting" in out and "2757" in out)
    e = _event_item(tg, "Swim")
    q.clear_pending_confirm(tg)
    out = rs.route(_envelope(tg, "yes"))
    check("bare yes, exactly one open → binds to it", q.get_item(e)["status"] in ("confirmed", "done"))
    f1 = _event_item(tg, "Guitar"); f2 = _event_item(tg, "Chess")
    q.clear_pending_confirm(tg)
    out = rs.route(_envelope(tg, "yes"))
    check("bare yes, several open → asks which one, listing hashes",
          "Which one?" in out and f1[:8] in out and f2[:8] in out and "Guitar" in out)
    check("…and touches neither", q.get_item(f1)["status"] == "awaiting_confirm"
          and q.get_item(f2)["status"] == "awaiting_confirm")

    # ── fallback names the message ─────────────────────────────────────────
    out = rs.route(_envelope(tg, "blorp fizzle", message_id="2760"))
    check("fallback carries the message id and the text",
          "didn't understand" in out and "2760" in out and "blorp fizzle" in out)
    check("_msg_ref truncates long text", rs._msg_ref("1", "x" * 80).count("x") == 39 and "…" in rs._msg_ref("1", "x" * 80))
    check("_msg_ref without an id still quotes the text", rs._msg_ref(None, "hi") == " _(“hi”)_")

    # ── two agent approvals (the /v1/approvals keyboard) in flight ────────
    def _approval(title, schedule_at=None):
        iid = q.write_approval_item(intent="linkedin_post", raw_message=title, sender=tg, channel_id=tg,
                           source="telegram", payload={"text": title}, approval_id=f"ap-{title}",
                           schedule_at=schedule_at)
        q.update_status(iid, "awaiting_confirm")
        return iid
    p1 = _approval("post one"); q.set_pending_confirm(tg, p1)
    p2 = _approval("post two"); q.set_pending_confirm(tg, p2)
    rs.route(_envelope(tg, f"yes #{p1[:8]}"))
    check("agent approvals: yes #<first> confirms the first even though the slot is on the second",
          q.get_item(p1)["status"] in ("confirmed", "done") and q.get_item(p2)["status"] == "awaiting_confirm")
    rs.route(_envelope(tg, f"cancel #{p2[:8]}"))
    check("agent approvals: cancel #<second> cancels only the second", q.get_item(p2)["status"] == "cancelled")

    # ── stale sweep ────────────────────────────────────────────────────────
    g = _event_item(tg, "Old")
    with q._connect() as conn:
        conn.execute("UPDATE queue_items SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?", (g,))
    n = q.expire_stale_awaiting(hours=24)
    check("stale awaiting_confirm items are cancelled by the sweep",
          n >= 1 and q.get_item(g)["status"] == "cancelled")
    check("…fresh ones are not", q.get_item(f1)["status"] == "awaiting_confirm")
    sch = _approval("later", schedule_at="2999-01-01T09:00:00Z")
    with q._connect() as conn:
        conn.execute("UPDATE queue_items SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?", (sch,))
    q.expire_stale_awaiting(hours=24)
    check("…nor an approval scheduled for the future", q.get_item(sch)["status"] == "awaiting_confirm")

    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all confirm-by-hash checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
