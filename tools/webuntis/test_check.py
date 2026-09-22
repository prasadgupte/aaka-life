#!/usr/bin/env python3
"""
tools/webuntis/test_check.py — day rendering, exams, and "new since last run".

2026-09-22: a parent asked why the digest didn't say school starts later when
lessons at the front of the day are gone, and asked for upcoming exams above
the homework list with ❗ the first time one shows up. The day-level facts
(late start, how many lessons are cancelled) used to be a trailing ❌ per line,
which is easy to skim past.

Pure-function tests — no network. Run: python3 tools/webuntis/test_check.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check as C  # noqa: E402

_FAILURES = []


def check(desc, cond):
    print(("  ok   " if cond else "  FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


def row(t0, t1, subj, room="014", code="", reason=""):
    return {"t0": t0, "t1": t1, "subj": subj, "room": room, "code": code, "reason": reason}


# A normal day's slot starts at this school (getTimegridUnits)
GRID = [800, 845, 950, 1035, 1150, 1305, 1400, 1450]


def test_day():
    full = [row("08:00", "09:30", "D"), row("09:50", "11:20", "Ma"),
            row("11:50", "12:35", "Gewi"), row("14:50", "15:35", "Schulclub", "RE1")]
    out = "\n".join(C._format_day(full, "*Mi 23.09*", [], "", GRID))
    check("normal day: header has no warning", out.splitlines()[0] == "*Mi 23.09*")
    check("normal day: start line is the span, no 'late'",
          "🕘 08:00–15:35" in out or "🕘 08:00–12:35" in out)
    check("normal day: no 'starts late' when the day begins at slot 1", "late" not in out)
    check("normal day: lessons are listed", "Ma" in out and "Gewi" in out)

    # first two slots missing → the fact the parent wants
    late = [row("09:50", "11:20", "E"), row("11:50", "12:35", "Gewi"),
            row("14:00", "14:45", "D")]
    out = "\n".join(C._format_day(late, "*Mi 23.09*", [], "", GRID))
    check("late start: says when school actually starts", "🕘 09:50–14:45" in out)
    check("late start: names how many periods are skipped", "starts 2 periods late" in out)
    single = [row("08:45", "09:30", "D")]
    check("late start: singular wording for one period",
          "starts 1 period late" in "\n".join(C._format_day(single, "h", [], "", GRID)))

    # cancellations
    partly = [row("08:00", "09:30", "D", code="cancelled"), row("09:50", "11:20", "Ma"),
              row("11:50", "12:35", "Gewi")]
    out = "\n".join(C._format_day(partly, "*Mi*", [], "", GRID))
    check("partly cancelled: header counts them", "⚠️ 1 of 3 lessons cancelled" in out)
    check("partly cancelled: start line skips the cancelled lesson", "🕘 09:50" in out)
    check("partly cancelled: the ❌ LEADS the line, not trails it",
          any(l.strip().startswith("❌ 08:00") for l in out.splitlines()))

    allgone = [row("08:00", "09:30", "D", code="cancelled"),
               row("09:50", "11:20", "Ma", code="cancelled")]
    out = "\n".join(C._format_day(allgone, "*Mi*", [], "", GRID))
    check("all cancelled: one loud line", "🎉 NO SCHOOL — all 2 lessons cancelled" in out)
    check("all cancelled: nothing else is printed", len(out.splitlines()) == 1)

    with_club = allgone + [row("14:50", "15:35", "Schulclub", "RE1")]
    out = "\n".join(C._format_day(with_club, "*Mi*", [], "", GRID))
    check("all lessons cancelled but a club runs: says so, and what is still on",
          "🚫 ALL 2 LESSONS CANCELLED" in out and "still on: 14:50–15:35 Schulclub" in out)
    check("a club is not a lesson", C._is_activity("Schulclub") and C._is_activity("AG-In")
          and not C._is_activity("Ma"))
    check("an after-school club alone doesn't count as a late start",
          "late" not in "\n".join(C._format_day([row("14:50", "15:35", "Schulclub", "RE1")],
                                                "h", [], "", GRID)))

    out = "\n".join(C._format_day(partly, "*Mi*", ["Wandertag"], " — Wandertag", GRID))
    check("reasons survive in the header", "Wandertag" in out)
    check("empty day renders nothing but the header",
          C._format_day([], "*Mi*", [], "", GRID) == ["*Mi*"])


def test_new_marking():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "exam_seen.json")
        keys = {"a", "b"}
        check("first run marks nothing new (no earlier run to compare to)",
              C._mark_new_keys(keys, path, 24) == set())
        check("…but records what it saw", set(json.load(open(path))) == keys)
        check("second run does NOT announce the bootstrap backlog as new",
              C._mark_new_keys(keys, path, 24) == set())
        check("an item is new on the very run it first appears",
              C._mark_new_keys(keys | {"c"}, path, 24) == {"c"})
        check("…and stays new on a re-run inside the window",
              C._mark_new_keys(keys | {"c"}, path, 24) == {"c"})
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=30)
        check("…and stops being new after the window",
              C._mark_new_keys(keys | {"c"}, path, 24, now=later) == set())
        check("no state dir → no marking, no crash", C._mark_new_keys(keys, "", 24) == set())


class _FakeSession:
    """Serves one weekly/data payload per ISO week."""

    def __init__(self, by_week):
        self.by_week = by_week
        self.calls = []

    def get(self, url, timeout=0, params=None, headers=None):
        self.calls.append(params.get("date"))
        week = dt.date.fromisoformat(params["date"]).isocalendar()[1]
        periods = self.by_week.get(week, [])

        class R:
            @staticmethod
            def json():
                return {"data": {"result": {"data": {"elementPeriods": {"4242": periods}}}}}
        return R()


def _exam_period(date_int, start, name, exam_id):
    return {"date": date_int, "startTime": start, "is": {"exam": True},
            "exam": {"id": exam_id, "name": name}}


def test_exams():
    me = {"personId": 4242, "personType": 5}
    today = dt.date.today()
    in7 = today + dt.timedelta(days=7)
    in21 = today + dt.timedelta(days=21)
    far = today + dt.timedelta(days=60)
    by_week = {
        in7.isocalendar()[1]: [
            {"date": int(in7.strftime("%Y%m%d")), "startTime": 950, "is": {"standard": True}},
            _exam_period(int(in7.strftime("%Y%m%d")), 800, "Deutsch", 9356),
            # the same exam spans two periods — one entry only, at its start
            _exam_period(int(in7.strftime("%Y%m%d")), 845, "Deutsch", 9356),
        ],
        in21.isocalendar()[1]: [_exam_period(int(in21.strftime("%Y%m%d")), 1035, "Mathe", 9400)],
        far.isocalendar()[1]: [_exam_period(int(far.strftime("%Y%m%d")), 800, "Weit weg", 9999)],
    }
    with tempfile.TemporaryDirectory() as td:
        os.environ["AAKA_CONFIG_DIR"] = td
        s = _FakeSession(by_week)
        r = C._exams(s, "https://x/WebUntis", me, "kid")     # first run: records only
        check("exams: both upcoming exams found", r["count"] == 2)
        check("exams: a double-period exam is listed once, at its start time",
              r["summary"].count("Deutsch") == 1 and "08:00" in r["summary"])
        check("exams: one beyond the horizon is not listed", "Weit weg" not in r["summary"])
        check("exams: soonest first", r["summary"].index("Deutsch") < r["summary"].index("Mathe"))
        check("exams: countdown in days", "(in 7 days)" in r["summary"])
        check("exams: German weekday + date", f"{C._WEEKDAY_DE[in7.weekday()]} {in7.strftime('%d.%m')}"
              in r["summary"])
        check("exams: first run marks nothing new", "❗" not in r["summary"])
        check("exams: one request per ISO week, not per day", len(s.calls) <= 6)

        r2 = C._exams(_FakeSession(by_week), "https://x/WebUntis", me, "kid")
        check("exams: the run after the bootstrap is still quiet", "❗" not in r2["summary"])

        by_week[in7.isocalendar()[1]].append(
            _exam_period(int(in7.strftime("%Y%m%d")), 1150, "Englisch", 9500))
        r3 = C._exams(_FakeSession(by_week), "https://x/WebUntis", me, "kid")
        check("exams: a newly announced exam is marked ❗ and bolded",
              "❗ *Englisch" in r3["summary"] and "(1 new ❗)" in r3["summary"])
        check("exams: the older ones stay plain", "• Deutsch" in r3["summary"])

        empty = C._exams(_FakeSession({}), "https://x/WebUntis", me, "kid")
        check("exams: none → empty summary, count 0 (digest omits the block)",
              empty["count"] == 0 and empty["summary"] == "")

        class Broken:
            def get(self, *a, **k):
                raise RuntimeError("network down")
        broke = C._exams(Broken(), "https://x/WebUntis", me, "kid")
        check("exams: a broken fetch never fails the digest", broke["count"] == 0 and broke["ok"])


def main():
    test_day()
    test_new_marking()
    test_exams()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all webuntis checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
