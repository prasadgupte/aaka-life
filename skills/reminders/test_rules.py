#!/usr/bin/env python3
"""
skills/reminders/test_rules.py — contextual reminder rules.

Offline and hermetic: a temp config dir, a hand-written day view, an explicit
date. No clock dependence, so this cannot pass on Tuesday and fail on Wednesday.

Run: python3 skills/reminders/test_rules.py   (exit 0 = pass)
"""
import datetime
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ["AAKA_CONFIG_DIR"] = tempfile.mkdtemp(prefix="aaka-reminders-test-")

from skills.reminders import rules  # noqa: E402

_FAILURES = []

MON = datetime.date(2026, 9, 7)    # a Monday
TUE = datetime.date(2026, 9, 8)

SPORT_DAY = """# Today
**0800** 🎒 School run
**1400** ⚽ Sport — Gym hall
"""
QUIET_DAY = """# Today
**0800** 🎒 School run
"""
HOLIDAY_DAY = """# Today
**0900** 🏖️ School holiday — no lessons
**1400** ⚽ Sport club (cancelled)
"""


def check(desc, cond):
    if cond:
        print(f"  ok   {desc}")
    else:
        print(f"  FAIL {desc}")
        _FAILURES.append(desc)


def reset():
    rules.save({})


def test_event_conditional_follows_the_calendar():
    """The headline case: a bag needed on days the calendar says sport, which
    keeps working when sport moves to another day."""
    reset()
    rules.add("🎒 Sport bag", member="ari", event_matches="sport")
    check("fires on a day with sport",
          rules.due(SPORT_DAY, date=TUE, member="ari") == ["🎒 Sport bag"])
    check("silent on a day without",
          rules.due(QUIET_DAY, date=TUE, member="ari") == [])
    check("day of week is irrelevant to it",
          rules.due(SPORT_DAY, date=MON, member="ari") == ["🎒 Sport bag"])


def test_weekday_rule():
    reset()
    rules.add("👟 Sports — regular shoes", member="rumi", weekday="mon")
    check("fires on Monday",
          rules.due(QUIET_DAY, date=MON, member="rumi") == ["👟 Sports — regular shoes"])
    check("silent on Tuesday",
          rules.due(QUIET_DAY, date=TUE, member="rumi") == [])


def test_unless_suppresses():
    reset()
    rules.add("🎒 Sport bag", member="ari", event_matches="sport",
              unless_matches="holiday|cancelled")
    check("suppressed when the day says holiday",
          rules.due(HOLIDAY_DAY, date=TUE, member="ari") == [])
    check("still fires on a normal sport day",
          rules.due(SPORT_DAY, date=TUE, member="ari") == ["🎒 Sport bag"])


def test_scoping_by_member():
    reset()
    rules.add("🎒 Sport bag", member="ari", event_matches="sport")
    check("not shown to another member",
          rules.due(SPORT_DAY, date=TUE, member="rumi") == [])
    check("household-wide view sees it",
          rules.due(SPORT_DAY, date=TUE, member="") == ["🎒 Sport bag"])


def test_enable_disable_and_remove():
    reset()
    res = rules.add("🎒 Sport bag", member="ari", event_matches="sport")
    rid = res["id"]
    rules.set_enabled(rid, False)
    check("a muted reminder does not fire",
          rules.due(SPORT_DAY, date=TUE, member="ari") == [])
    rules.set_enabled(rid, True)
    check("unmuting restores it",
          rules.due(SPORT_DAY, date=TUE, member="ari") == ["🎒 Sport bag"])
    check("remove reports success", rules.remove(rid) is True)
    check("removing twice is not an error", rules.remove(rid) is False)
    check("nothing fires afterwards",
          rules.due(SPORT_DAY, date=TUE, member="ari") == [])


def test_bad_rules_are_refused_not_silently_broken():
    reset()
    for kwargs, why in [
        ({"text": ""}, "empty text"),
        ({"text": "x"}, "no condition"),
        ({"text": "x", "weekday": "someday"}, "unknown weekday"),
        ({"text": "x", "event_matches": "([unclosed"}, "invalid regex"),
    ]:
        try:
            rules.add(**kwargs)
            check(f"refuses {why}", False)
        except ValueError:
            check(f"refuses {why}", True)


def test_render_is_empty_when_nothing_fires():
    """An ordinary day's summary must be byte-identical to before the feature."""
    reset()
    rules.add("🎒 Sport bag", member="ari", event_matches="sport")
    check("no block on a quiet day", rules.render(QUIET_DAY, date=TUE, member="ari") == "")
    out = rules.render(SPORT_DAY, date=TUE, member="ari")
    check("block appears on a sport day", "Don't forget" in out and "🎒 Sport bag" in out)


def main():
    test_event_conditional_follows_the_calendar()
    test_weekday_rule()
    test_unless_suppresses()
    test_scoping_by_member()
    test_enable_disable_and_remove()
    test_bad_rules_are_refused_not_silently_broken()
    test_render_is_empty_when_nothing_fires()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all reminder rule checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
