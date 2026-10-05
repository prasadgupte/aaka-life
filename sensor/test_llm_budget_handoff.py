#!/usr/bin/env python3
"""
sensor/test_llm_budget_handoff.py — when the cloud LLM is out of budget, a /cal
is handed to the home executor instead of failing after nine retries.

2026-09-21: every /cal on the sensor died with "Gemini overloaded (HTTP 429)
… (pass 3/3)". The 429 body said "exceeded its monthly spending cap" — not
load, a cap that only clears on the 1st — and the provider retried 3 models ×
3 passes before giving up, then the user was told to try again. Now:
  1. a spend-cap / daily-quota 429 raises LLMBudgetExceeded on the first call;
  2. the adapter falls back to a local `claude` where one exists (the Mac);
  3. the sensor (no claude) queues `add_event_home`; the executor extracts,
     writes the add_event item as awaiting_confirm and sends the preview;
  4. vps_sync ships the awaiting_confirm item + pending_confirm to the VPS and
     adopts the sensor's 'confirmed' (with edited payload) on the way back.
Run: python3 sensor/test_llm_budget_handoff.py   (exit 0 = pass)
"""
import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ["AAKA_CONFIG_DIR"] = str(REPO_ROOT / "samples" / "demo")
_TMP = tempfile.mkdtemp(prefix="aaka-budget-")
os.environ["QUEUE_DB"] = str(Path(_TMP) / "butler.db")
os.environ["SIGNAL_LINKED_MODE"] = "false"
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ.pop("LLM_PROVIDER", None)
os.environ["AAKA_ROLE"] = "sensor"   # the sensor's view: Gemini direct, no gateway, no local claude

import aaka_config  # noqa: E402
from gateway import llm_providers as lp  # noqa: E402
from gateway.adapter import GatewayAdapter  # noqa: E402
from aaka_queue import queue as q  # noqa: E402

_FAILURES = []


def check(desc, cond):
    print(("  ok   " if cond else "  FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


def _http_error(code: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": code, "message": message, "status": "RESOURCE_EXHAUSTED"}}).encode()
    return urllib.error.HTTPError("https://x", code, "Too Many Requests", {}, io.BytesIO(body))


def test_provider():
    calls = []
    real_open, real_sleep = lp.urllib.request.urlopen, lp.time.sleep
    lp.time.sleep = lambda *_: None

    def cap_open(req, timeout=0):
        calls.append(req.full_url)
        raise _http_error(429, "Your project has exceeded its monthly spending cap. Please go to AI Studio…")
    lp.urllib.request.urlopen = cap_open
    try:
        try:
            lp.gemini("hi")
            check("spend cap raises", False)
        except lp.LLMBudgetExceeded as e:
            check("spend-cap 429 raises LLMBudgetExceeded", True)
            check("…on the FIRST call, no model/pass retries", len(calls) == 1)
            check("…carrying the provider's own message", "spending cap" in e.detail)
            check("…and naming the provider", e.provider == "Gemini")
    finally:
        lp.urllib.request.urlopen = real_open

    calls.clear()

    def busy_open(req, timeout=0):
        calls.append(req.full_url)
        raise _http_error(429, "Resource has been exhausted (e.g. check quota).")
    lp.urllib.request.urlopen = busy_open
    try:
        try:
            lp.gemini("hi")
            check("plain 429 raises", False)
        except lp.LLMBudgetExceeded:
            check("a plain 'exhausted' 429 is NOT treated as a budget error", False)
        except RuntimeError as e:
            check("a plain 429 still walks every model and pass", len(calls) == 3 * 3)
            check("…and raises the overloaded error", "overloaded" in str(e))
    finally:
        lp.urllib.request.urlopen = real_open
        lp.time.sleep = real_sleep

    daily = _http_error(429, "Quota exceeded for quota metric 'Generate requests per day'")
    check("a per-day quota message counts as budget", lp._is_budget_error(lp._gemini_error_message(daily)))


def test_adapter_fallback():
    ad = GatewayAdapter()
    ad._log_llm_usage = lambda *a, **k: None
    real_complete, real_which = lp.complete, lp.find_claude

    def complete(prompt, timeout, provider=None, images=None):
        if provider == "gemini":
            raise lp.LLMBudgetExceeded("Gemini", "monthly spending cap")
        if provider == "claude-cli":
            return "from-claude"
        raise AssertionError(provider)
    lp.complete = complete
    from gateway import llm_policy
    live_cal = llm_policy.scope("add_event")   # the one intent allowed to call Gemini directly
    live_cal.__enter__()
    try:
        lp.find_claude = lambda: "/opt/homebrew/bin/claude"
        check("adapter: budget error + local claude → claude-cli answers",
              ad.call_llm("hi") == "from-claude")
        lp.find_claude = lambda: None
        try:
            ad.call_llm("hi")
            check("adapter: no claude → error propagates", False)
        except lp.LLMBudgetExceeded:
            check("adapter: no local claude → LLMBudgetExceeded propagates (sensor case)", True)
        lp.find_claude = lambda: "/opt/homebrew/bin/claude"
        os.environ["AAKA_LLM_LOCAL_FALLBACK"] = "0"
        try:
            ad.call_llm("hi")
            check("adapter: fallback can be switched off", False)
        except lp.LLMBudgetExceeded:
            check("adapter: AAKA_LLM_LOCAL_FALLBACK=0 disables the rescue", True)
        os.environ.pop("AAKA_LLM_LOCAL_FALLBACK", None)
        check("images never fall back to claude-cli (no vision)",
              lp.local_fallback_provider("gemini", images=[{"data": "x"}]) is None)
    finally:
        live_cal.__exit__(None, None, None)
        lp.complete, lp.find_claude = real_complete, real_which


def _envelope(sender: str, text: str) -> str:
    meta = {"chat_id": f"telegram:{sender}", "message_id": "77",
            "sender_id": sender, "conversation_label": f"id:{sender}"}
    return "Conversation info (untrusted metadata):\n```json\n" + json.dumps(meta) + "\n```\n" + text


def test_sensor_handoff():
    import sensor.router_sensor as rs
    aaka_config._load.cache_clear()
    member = next(m for m in aaka_config.members() if m.get("telegram") or m.get("telegram_id"))
    tg = str(member.get("telegram") or member.get("telegram_id"))
    rs._react_read = lambda *a, **k: None

    real_extract = rs._extract_events_batch

    def capped(*a, **k):
        raise lp.LLMBudgetExceeded("Gemini", "monthly spending cap")
    rs._extract_events_batch = capped
    try:
        out = rs.route(_envelope(tg, "/cal Alex @ Dentist 3 Oct 10:00"))
    finally:
        rs._extract_events_batch = real_extract
    check("sensor: /cal under a budget error is handed home, not 'Extraction failed'",
          "home machine" in out and "Extraction failed" not in out)
    conn = q._connect()
    rows = conn.execute("SELECT intent, status, payload FROM queue_items WHERE intent='add_event_home'").fetchall()
    check("sensor: one add_event_home item queued", len(rows) == 1)
    if rows:
        pl = json.loads(rows[0]["payload"])
        check("sensor: item is 'confirmed' so the executor picks it up", rows[0]["status"] == "confirmed")
        check("sensor: payload carries the raw text + sender + message_id",
              pl.get("raw_message", "").startswith("/cal") and pl.get("sender") == tg
              and str(pl.get("message_id")) == "77")

    def broken(*a, **k):
        raise RuntimeError("Gemini overloaded (HTTP 503) on x (pass 3/3)")
    rs._extract_events_batch = broken
    try:
        out = rs.route(_envelope(tg, "/cal Alex @ Dentist 3 Oct 10:00"))
    finally:
        rs._extract_events_batch = real_extract
    check("sensor: a transient overload is still reported, not handed home",
          "Extraction failed" in out)
    n = conn.execute("SELECT count(*) FROM queue_items WHERE intent='add_event_home'").fetchone()[0]
    check("sensor: …and queues nothing", n == 1)


def test_executor_handoff():
    import executor.queue_worker as qw
    import sensor.router_sensor as rs
    real_build = rs.build_add_event_item

    def fake_build(message, sender_id, sender_email="", *, me_flag=False, message_id=""):
        return "add_event", {"summary": "Dentist", "attendee": "Alex", "occurrences": [],
                             "message_id": message_id}, "📅 Alex @ Dentist — Fri 3 Oct 10:00"
    rs.build_add_event_item = fake_build
    try:
        res = qw._exec_add_event_home({"raw_message": "/cal Alex @ Dentist", "sender": "101010001",
                                       "channel_id": "101010001", "source": "telegram",
                                       "message_id": "77", "sender_email": "", "me_flag": False})
    finally:
        rs.build_add_event_item = real_build
    check("executor: returns the new item id", bool(res.get("ok")) and res.get("item_id"))
    conn = q._connect()
    item = conn.execute("SELECT * FROM queue_items WHERE id = ?", (res.get("item_id", ""),)).fetchone()
    check("executor: add_event item written as awaiting_confirm",
          item is not None and item["intent"] == "add_event" and item["status"] == "awaiting_confirm")
    ob = conn.execute("SELECT * FROM outbox_items WHERE channel_id='101010001' ORDER BY created_at DESC").fetchone()
    check("executor: preview goes to the outbox with Yes/Cancel", ob is not None and ob["reply_markup"]
          and "Dentist" in ob["text"] and "🏠" in ob["text"])
    pc = q.get_pending_confirm("101010001")
    check("executor: pending_confirm armed for the sender on that item",
          pc is not None and pc.get("item_id") == res.get("item_id"))

    # the built item is byte-identical in shape to the sensor's: the sensor's
    # confirm path finds intent add_event + payload
    def no_events(*a, **k):
        raise rs.NoEventsParsed("x")
    rs.build_add_event_item = no_events
    try:
        res2 = qw._exec_add_event_home({"raw_message": "/cal ???", "sender": "101010002",
                                        "channel_id": "101010002", "source": "telegram"})
    finally:
        rs.build_add_event_item = real_build
    ob2 = conn.execute("SELECT text FROM outbox_items WHERE channel_id='101010002'").fetchone()
    check("executor: nothing parsed → says so, no item, no pending_confirm",
          not res2.get("ok") and ob2 and "Could not parse" in ob2["text"]
          and q.get_pending_confirm("101010002") is None)


def test_sync_merge():
    import executor.vps_sync as vs
    conn = q._connect()
    # local: awaiting_confirm item the executor created
    item_id = q.write_item(intent="add_event", raw_message="/cal x", sender="101010001",
                           channel_id="101010001", source="telegram", payload={"summary": "old"})
    q.update_status(item_id, "awaiting_confirm")
    # push: awaiting_confirm rows are now candidates
    pushed = {}
    real_push, real_ssh = vs._push_to_vps, vs._ssh_run
    vs._push_to_vps = lambda cfg, table, rows, mode="insert", pk="id": pushed.setdefault(table, []).extend(rows) or len(rows)
    cfg = vs.SyncConfig(vps_host="x", vps_db_path="/x.db", config_dir=_TMP, vps_config_root="/x")
    try:
        vs._set_hwm("push_hwm", "1970-01-01T00:00:00Z")
        vs._push_status_updates(cfg)
        ids = [r["id"] for r in pushed.get("queue_items", [])]
        check("sync: executor-side awaiting_confirm item is pushed to the VPS", item_id in ids)
        pushed.clear()
        vs._push_status_updates(cfg)
        check("sync: …but only once (HWM), so a later sensor 'confirmed' is not clobbered",
              not pushed.get("queue_items"))

        # pull: the VPS now says confirmed with an edited payload
        row = dict(conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone())
        row["status"] = "confirmed"
        row["payload"] = json.dumps({"summary": "edited by yes 1 3"})
        row["updated_at"] = "2999-01-01T00:00:00Z"

        class R:
            returncode = 0
            stdout = json.dumps([row])
            stderr = ""
        vs._ssh_run = lambda *a, **k: R()
        vs._set_hwm("pull_hwm", "1970-01-01T00:00:00Z")
        vs._pull_queue_rows(cfg)
        got = conn.execute("SELECT status, payload FROM queue_items WHERE id = ?", (item_id,)).fetchone()
        check("sync: sensor 'confirmed' is adopted on a local awaiting_confirm item", got["status"] == "confirmed")
        check("sync: …together with the sensor-edited payload", "edited" in got["payload"])
        check("sync: it is now what the executor consumes", any(i["id"] == item_id for i in q.read_pending()))

        # the sensor executed it itself (VPS-direct calendar write): mirror 'done'
        item2 = q.write_item(intent="add_event", raw_message="/cal y", sender="101010001",
                             channel_id="101010001", source="telegram", payload={"summary": "y"})
        q.update_status(item2, "awaiting_confirm")
        row2 = dict(conn.execute("SELECT * FROM queue_items WHERE id = ?", (item2,)).fetchone())
        row2.update(status="done", updated_at="2999-01-02T00:00:00Z")
        R.stdout = json.dumps([row2])
        vs._pull_queue_rows(cfg)
        got2 = conn.execute("SELECT status FROM queue_items WHERE id = ?", (item2,)).fetchone()
        check("sync: a sensor-side 'done' is mirrored onto a local awaiting_confirm item (no ghost rows)",
              got2["status"] == "done")
    finally:
        vs._push_to_vps, vs._ssh_run = real_push, real_ssh


def test_sensor_gemini_scope():
    """2026-10-05: on the sensor only the live /cal preview may call Gemini;
    a task is saved as typed instead of costing a Gemini call."""
    import sensor.router_sensor as rs
    from gateway import llm_policy
    from skills.tasks.prepare_task import prepare_task
    os.environ.pop("AAKA_GEMINI_INTENTS", None)
    real_complete, real_which = lp.complete, lp.find_claude
    seen = []

    def record(prompt, timeout, provider=None, images=None):
        seen.append((provider, llm_policy.current_scope()))
        raise lp.LLMBudgetExceeded("Gemini", "monthly spending cap")
    lp.complete = record
    lp.find_claude = lambda: None   # the VPS has no local claude to rescue with
    try:
        t = prepare_task("call the plumber #home")
        check("sensor: /task makes no Gemini call", seen == [])
        check("sensor: /task keeps the text as the title, minus #tags", t["title"] == "call the plumber")
        check("sensor: /task is flagged no_llm (reply explains /edit due)", t.get("no_llm") is True)
        try:
            rs._extract_events_batch("/cal Alex @ Dentist 3 Oct 10:00")
        except lp.LLMBudgetExceeded:
            pass
        check("sensor: /cal still reaches Gemini, inside the add_event scope",
              seen and seen[-1] == ("gemini", "add_event"))
        try:
            GatewayAdapter().call_llm("hello")
            check("sensor: an unscoped LLM call is refused", False)
        except llm_policy.GeminiNotAllowed:
            check("sensor: an unscoped LLM call is refused before any request", len(seen) == 1)

        from sensor.intents.admin import handle as admin_handle
        out = admin_handle("llm_call", "/llm say hi", "x", "x", "telegram")
        check("sensor: /llm is off by default and says how to switch it on",
              "AAKA_GEMINI_INTENTS" in out and len(seen) == 1)

        # The operator adds a service: /task then goes to Gemini under its own scope.
        os.environ["AAKA_GEMINI_INTENTS"] = "add_event,add_task"
        try:
            rs._extract_task("/task call the plumber friday")
        except RuntimeError:
            pass   # the fake provider is out of budget; reaching it is the point
        check("sensor: AAKA_GEMINI_INTENTS=…,add_task lets /task call Gemini (scope add_task)",
              seen[-1] == ("gemini", "add_task"))
        os.environ.pop("AAKA_GEMINI_INTENTS", None)
    finally:
        lp.complete, lp.find_claude = real_complete, real_which


def main():
    test_provider()
    test_adapter_fallback()
    test_sensor_gemini_scope()
    test_sensor_handoff()
    test_executor_handoff()
    test_sync_merge()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all llm-budget hand-off checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
