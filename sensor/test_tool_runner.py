#!/usr/bin/env python3
"""sensor/test_tool_runner.py — aaka Tools runner (no network for the core checks)."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="aaka-tools-"))
(_TMP / "config").mkdir(parents=True, exist_ok=True)
(_TMP / "logs").mkdir(parents=True, exist_ok=True)
(_TMP / "config" / "aaka.yaml").write_text(
    "system: {bot_name: 'Rosi', timezone: 'Europe/Berlin'}\nmembers:\n"
    "  - {id: 'alex', name: 'Alex', admin: true, telegram: '111'}\n"
    "  - {id: 'kid', name: 'Kid', whatsapp: '+491700000000', signal: 'uuid-kid'}\n"
    "  - {id: 'nobody', name: 'Nobody'}\n"
)
os.environ["ENABLED_CHANNELS"] = "telegram,signal"
os.environ["AAKA_CONFIG_DIR"] = str(_TMP)

# a mock tool that emits a structured result
_mock = _TMP / "mock_tool.py"
_mock.write_text(
    "import json,os\n"
    "print(json.dumps({'ok': os.environ.get('AAKA_TOOL_SECRETS','')!='', "
    "'summary': 'secrets='+os.environ.get('AAKA_TOOL_SECRETS','none'), 'error': None}))\n"
)
(_TMP / "config" / "tools.yaml").write_text(
    f"mock:\n  run: {_mock}\n  secrets: mocksecret\n  report_to: alex\n  enabled: true\n"
    f"disabled_one:\n  run: {_mock}\n  enabled: false\n"
)

import sensor.tool_runner as tr  # noqa: E402

_FAIL = []
def check(d, c):
    print(f"  {'ok  ' if c else 'FAIL'} {d}")
    if not c:
        _FAIL.append(d)


def main():
    man = tr.load_manifest()
    check("manifest loads tools.yaml", "mock" in man and "disabled_one" in man)

    res = tr.run_tool("mock", man["mock"])
    check("run_tool parses structured result", res.get("ok") is True)
    check("secrets dir injected as AAKA_TOOL_SECRETS", "mocksecret" in res.get("summary", ""))

    logp = _TMP / "logs" / "tools" / "mock.jsonl"
    check("run logged to logs/tools/<name>.jsonl", logp.exists() and "ok" in logp.read_text())

    disabled = tr.run_and_report("disabled_one")
    check("disabled tool not run", disabled.get("error") == "disabled")

    unknown = tr.run_tool("nope", {})
    check("unknown tool → no_such_tool", unknown.get("error") == "no_such_tool")

    # cron matcher
    d = datetime(2026, 9, 2, 7, 0)          # Wed 07:00
    check("cron '0 7 * * *' matches 07:00", tr._cron_matches("0 7 * * *", d))
    check("cron '*/30 * * * *' matches :00", tr._cron_matches("*/30 * * * *", d))
    check("cron '0 8 * * *' does not match 07:00", not tr._cron_matches("0 8 * * *", d))
    check("cron dow: Wed is 3", tr._cron_matches("0 7 * * 3", d))

    # ── delivery: preferred enabled channel, list recipients, report_ok ──
    import aaka_config
    aaka_config._load.cache_clear()
    sent = []
    import message_send
    orig_send = message_send.send
    message_send.send = lambda text, channel=None, to=None, **kw: sent.append((channel, to, text)) or True
    try:
        check("_recipients: string → [id]", tr._recipients("alex") == ["alex"])
        check("_recipients: 'a, b' → [a, b]", tr._recipients("alex, kid") == ["alex", "kid"])
        check("_recipients: list passes through", tr._recipients(["alex", "kid"]) == ["alex", "kid"])
        os.environ["AAKA_ROLE"] = "sensor"
        tr._send(["alex", "kid", "nobody", "ghost"], "hello")
        check("telegram member reached on telegram", ("telegram", "111", "hello") in sent)
        check("whatsapp+signal member reached on signal (whatsapp not enabled)",
              ("signal", "uuid-kid", "hello") in sent)
        check("members with no enabled handle are skipped, unknown ids ignored", len(sent) == 2)
        sent.clear()
        queued = []
        import aaka_queue.queue as q
        orig_wo = q.write_outbox
        q.write_outbox = lambda **kw: queued.append(kw)
        try:
            os.environ["AAKA_ROLE"] = "executor"
            tr._send(["alex", "kid"], "hi")
            check("executor: telegram still direct", sent == [("telegram", "111", "hi")])
            check("executor: signal goes to the outbox for the sensor to flush",
                  len(queued) == 1 and queued[0]["channel_id"] == "uuid-kid" and queued[0]["source"] == "signal")
        finally:
            q.write_outbox = orig_wo
            os.environ["AAKA_ROLE"] = "sensor"
        sent.clear()
        tr.report("mock", {"report_to": ["alex", "kid"]}, {"ok": True, "summary": "did it"})
        check("report_to list → every recipient", {c for c, _, _ in sent} == {"telegram", "signal"})
        sent.clear()
        tr.report("mock", {"report_to": "alex", "report_ok": False}, {"ok": True, "summary": "did it"})
        check("report_ok: false → nothing on success", sent == [])
        tr.report("mock", {"report_to": "kid", "report_ok": False}, {"ok": False, "error": "boom", "summary": "x"})
        check("report_ok: false still alerts the admin on error", sent and sent[0][0] == "telegram")
    finally:
        message_send.send = orig_send

    # ── run target: .sh runs under bash ──
    sh = _TMP / "mock.sh"
    sh.write_text("#!/bin/bash\necho '{\"ok\": true, \"summary\": \"from bash\", \"error\": null}'\n")
    res = tr.run_tool("shtool", {"run": str(sh)})
    check(".sh target runs under bash", res.get("ok") is True and res.get("summary") == "from bash")

    # ── schedule in the family's timezone + catch-up of the latest missed slot ──
    from datetime import timezone as _tz, timedelta
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    check("_now_local is in the configured timezone", tr._now_local().tzinfo.key == "Europe/Berlin")
    now = datetime(2026, 9, 14, 9, 30, tzinfo=berlin)      # Mon 09:30 Berlin
    slot = tr._last_slot("45 18 * * 1-5", now)
    check("_last_slot finds Friday 18:45 from Monday 09:30",
          slot == datetime(2026, 9, 11, 18, 45, tzinfo=berlin))
    check("_last_slot None when nothing in the window", tr._last_slot("0 3 1 1 *", now) is None)

    runs = []
    orig_rar = tr.run_and_report
    tr.run_and_report = lambda name, extra_args="", catch_up="": runs.append((name, catch_up)) or {"ok": True}
    try:
        (_TMP / "config" / "tools.yaml").write_text(
            f"never_ran:\n  run: {_mock}\n  schedule: '45 18 * * 1-5'\n  placement: executor\n"
            f"stale:\n  run: {_mock}\n  schedule: '45 18 * * 1-5'\n  placement: executor\n"
            f"fresh:\n  run: {_mock}\n  schedule: '45 18 * * 1-5'\n  placement: executor\n"
            f"other_side:\n  run: {_mock}\n  schedule: '45 18 * * 1-5'\n  placement: sensor\n"
            f"live:\n  run: {_mock}\n  schedule: '30 9 * * *'\n  placement: executor\n"
        )
        logs = _TMP / "logs" / "tools"
        (logs / "stale.jsonl").write_text(json.dumps({"ts": "2026-09-10T16:45:03Z", "ok": True}) + "\n")   # Thu
        (logs / "fresh.jsonl").write_text(json.dumps({"ts": "2026-09-11T16:45:02Z", "ok": True}) + "\n")   # Fri 18:45 Berlin
        ran = tr.run_due("executor", now=now)
        names = dict(runs)
        check("tool matching now runs normally", names.get("live") == "")
        check("tool that missed its last slot is caught up once, flagged with the slot",
              names.get("stale") == "2026-09-11T18:45+02:00")
        check("tool that already ran at its last slot is left alone", "fresh" not in names)
        check("never-run tool is not fired by catch-up", "never_ran" not in names)
        check("other side's tools untouched", "other_side" not in names)
        check("run_due returns one entry per run", len(ran) == 2)
    finally:
        tr.run_and_report = orig_rar

    # report path is best-effort and must never raise (no telegram token here)
    try:
        tr.report("mock", man["mock"], {"ok": False, "error": "auth_required", "summary": "login failed"})
        check("report() never raises (auth_required path)", True)
    except Exception as e:
        check(f"report() never raises: {e}", False)

    print()
    if _FAIL:
        print(f"FAILED: {len(_FAIL)}")
        return 1
    print("all tool_runner checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
