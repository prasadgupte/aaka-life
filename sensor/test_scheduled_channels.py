#!/usr/bin/env python3
"""
sensor/test_scheduled_channels.py — scheduled pushes reach every member on their
preferred *enabled* channel, not just the ones with Telegram.

Run: python3 sensor/test_scheduled_channels.py   (exit 0 = pass)
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="aaka-sched-"))
for d in ("config", "data/calendar", "data/queue", "logs"):
    (_TMP / d).mkdir(parents=True, exist_ok=True)
(_TMP / "config" / "aaka.yaml").write_text(
    "system: {bot_name: 'Rosi', timezone: 'Europe/Berlin'}\nmembers:\n"
    "  - {id: 'mum', name: 'Mum', admin: true, telegram: '111', whatsapp: '+491', signal: 'uuid-mum'}\n"
    "  - {id: 'kid', name: 'Kid', whatsapp: '+492', signal: 'uuid-kid'}\n"
    "  - {id: 'gran', name: 'Gran', whatsapp: '+493'}\n"
)
(_TMP / "data" / "calendar" / "weekly.md").write_text("# This Week\n**Monday** 14 September 2026\n🏠 dentist\n")
(_TMP / "data" / "calendar" / "weekly_kid.md").write_text("# This Week — Kid\n**Monday** 14 September 2026\n🥋 taekwondo\n")
os.environ["AAKA_CONFIG_DIR"] = str(_TMP)
os.environ["ENABLED_CHANNELS"] = "telegram,signal"
os.environ.pop("TELEGRAM_GROUP_ID", None)

import aaka_config  # noqa: E402
import sensor.scheduled_summaries as ss  # noqa: E402

_FAIL = []


def check(d, c):
    print(f"  {'ok  ' if c else 'FAIL'} {d}")
    if not c:
        _FAIL.append(d)


def main():
    aaka_config._load.cache_clear()

    t = aaka_config.member_targets()
    check("telegram wins when a member has several handles", t.get("mum") == ("111", "telegram"))
    check("whatsapp skipped when not enabled → signal", t.get("kid") == ("uuid-kid", "signal"))
    check("member reachable only on a disabled channel is not a target", "gran" not in t)
    check("with whatsapp enabled, whatsapp beats signal",
          aaka_config.member_targets(["telegram", "whatsapp", "signal"]).get("kid") == ("+492", "whatsapp"))
    check("preferred_handle → ('', '') when unreachable",
          aaka_config.preferred_handle({"id": "x", "whatsapp": "+1"}) == ("", ""))

    queued = []
    ss._send_to_outbox = lambda channel_id, text, source="telegram", dry_run=False, reply_markup=None: \
        queued.append((channel_id, source, text))
    ss.analyze_fix = lambda *a, **k: []
    ss.merge_fixes_inline = lambda content, issues: content

    ss.send_saturday_heads_up()
    check("Saturday heads-up: one push per reachable member", len(queued) == 2)
    check("…the Telegram member on telegram", ("111", "telegram") in {(c, s) for c, s, _ in queued})
    check("…the Signal-only kid on signal", ("uuid-kid", "signal") in {(c, s) for c, s, _ in queued})
    kid_text = next(txt for c, s, txt in queued if c == "uuid-kid")
    check("…with their own weekly file", "taekwondo" in kid_text and "dentist" not in kid_text)

    queued.clear()
    ss.send_sunday_overview()
    check("Sunday overview: same fan-out", {(c, s) for c, s, _ in queued} == {("111", "telegram"), ("uuid-kid", "signal")})

    # nudge engine: a member with no overdue tasks of their own is left alone
    from skills.engagement import nudge_engine as ne
    import skills.tasks.local_tasks as lt
    calls = []
    lt.list_open = lambda **kw: calls.append(kw) or ([] if kw.get("owner") else [{"title": "someone else's"}])
    ne._local_hour = lambda m: 11
    ne._local_weekday = lambda m: 6
    ne._opt_out = lambda m: False
    ne._in_sleep_hours = lambda m: False
    import skills.engagement.engagement_db as edb
    edb.nudged_today = lambda m: False
    edb.get_state = lambda m: {"level": 0}
    edb.days_since_active = lambda m: 0
    edb.hours_since_nudge = lambda m: 99
    import skills.engagement.conflict_alert as ca
    ca.build_conflict_alerts = lambda m: []
    decision = ne.decide("kid")
    check("overdue nudge: no family-wide fallback (list_open only asked with owner)",
          all(kw.get("owner") == "kid" for kw in calls) and calls)
    check("…so a member without own overdue tasks gets no overdue nudge",
          decision is None or decision.nudge_type != "overdue_task_nudge")
    check("weekly digest retired: Sunday 18:00 yields none",
          decision is None or decision.nudge_type != "weekly_digest")

    print()
    if _FAIL:
        print(f"FAILED: {len(_FAIL)}")
        return 1
    print("all scheduled-channel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
