#!/usr/bin/env python3
"""
skills/calendar/test_scope.py — a non-admin member's calendar views carry only
their own events; admins keep the whole family calendar.

Before this, `_build_cal_to_members` mapped the family calendar to *every*
member, so a kid's today_<id>.md / weekly_<id>.md — and the 07:00, 21:00 and
weekend pushes built from them — were the parents' full view. The pushes also
fell back to the family-wide text whenever a member's own file was empty.
Run: python3 skills/calendar/test_scope.py   (exit 0 = pass)
"""
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ["AAKA_CONFIG_DIR"] = str(REPO_ROOT / "samples" / "demo")

import aaka_config  # noqa: E402
from skills.calendar import scope  # noqa: E402

_FAILURES = []


def check(desc, cond):
    print(("  ok   " if cond else "  FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


def _ev(summary="", description="", attendees=()):
    return {"summary": summary, "description": description,
            "attendees": [{"email": e} for e in attendees]}


def main():
    aaka_config._load.cache_clear()
    roster = {m["id"]: m for m in aaka_config.members()}
    admin = next(m for m in aaka_config.members() if m.get("admin"))
    kid = next(m for m in aaka_config.members() if m.get("role") == "child")
    household = aaka_config.group_name()

    # ── scope defaults ─────────────────────────────────────────────────────
    check("admin defaults to scope 'all'", aaka_config.member_calendar_scope(admin["id"]) == "all")
    check("child defaults to scope 'mine'", aaka_config.member_calendar_scope(kid["id"]) == "mine")
    grand = next((m for m in aaka_config.members() if m.get("role") == "grandparent"), None)
    if grand:
        check("grandparent (non-admin) defaults to 'mine'",
              aaka_config.member_calendar_scope(grand["id"]) == "mine")
    fam = aaka_config.group_member_id()
    if fam:
        check("household pseudo-member is 'all' (it is the family view)",
              aaka_config.member_calendar_scope(fam) == "all")
    check("unknown member id → 'mine' (safe default)",
          aaka_config.member_calendar_scope("nobody") == "mine")

    # explicit override wins in both directions
    kid_all = dict(kid, calendar_scope="all")
    admin_mine = dict(admin, calendar_scope="mine")
    real_members = aaka_config.members
    aaka_config.members = lambda: [kid_all, admin_mine]
    try:
        check("calendar_scope: all on a child overrides the default",
              aaka_config.member_calendar_scope(kid["id"]) == "all")
        check("calendar_scope: mine on an admin overrides the default",
              aaka_config.member_calendar_scope(admin["id"]) == "mine")
    finally:
        aaka_config.members = real_members

    # ── event_involves_member ──────────────────────────────────────────────
    name = kid["name"]
    check("name in title → involved",
          scope.event_involves_member(_ev(f"🧸 {name} @ Playdate"), kid))
    check("name in description only → involved",
          scope.event_involves_member(_ev("Dentist", f"for {name}, bring card"), kid))
    check("case-insensitive name match",
          scope.event_involves_member(_ev(name.upper() + " swim"), kid))
    check("name as a substring of another word is NOT a match",
          not scope.event_involves_member(_ev(f"{name}son family reunion"), kid))
    check("someone else's event → not involved",
          not scope.event_involves_member(_ev(f"{admin['name']} @ Dentist"), kid))
    check("untitled event → nobody's",
          not scope.event_involves_member(_ev("", ""), kid))
    kid_mail = dict(kid, email="kid@example.com")
    check("member email on the attendee list → involved",
          scope.event_involves_member(_ev("School meeting", attendees=["KID@example.com"]), kid_mail))
    check("empty member email never matches an attendee-less event",
          not scope.event_involves_member(_ev("School meeting", attendees=[""]), dict(kid, email="")))
    check("household-addressed event → everyone's",
          scope.event_involves_member(_ev(f"🚄 {household} @ Trip"), kid, household_name=household))
    check("household name only in the description does not widen scope",
          not scope.event_involves_member(_ev("Board meeting", f"{household} invited"), kid,
                                          household_name=household))

    # ── members_for_event: the sync-side filter ────────────────────────────
    everyone = list(roster)
    ev_admin = _ev(f"{admin['name']} @ Dentist")
    seen = scope.members_for_event(ev_admin, everyone, [], roster=roster, household_name=household)
    check("admin sees an event that names only them", admin["id"] in seen)
    check("kid does NOT see the admin's dentist", kid["id"] not in seen)
    other_kid = next((m["id"] for m in aaka_config.members()
                      if m.get("role") == "child" and m["id"] != kid["id"]), None)
    if other_kid:
        check("sibling does NOT see it either", other_kid not in seen)

    ev_kid = _ev(f"{name} @ Football")
    seen = scope.members_for_event(ev_kid, everyone, [], roster=roster, household_name=household)
    check("kid sees their own football", kid["id"] in seen)
    check("admin sees the kid's football too (scope all)", admin["id"] in seen)
    if other_kid:
        check("sibling does not see the kid's football", other_kid not in seen)

    ev_none = _ev("Plumber")
    seen = scope.members_for_event(ev_none, everyone, [], roster=roster, household_name=household)
    check("an event naming nobody reaches admins only",
          admin["id"] in seen and kid["id"] not in seen)

    seen = scope.members_for_event(ev_none, [kid["id"]], [kid["id"]], roster=roster, household_name=household)
    check("an event on the member's OWN calendar is theirs regardless of title",
          seen == [kid["id"]])

    # ── member_calendar_file: no family-wide fallback for a scoped member ───
    real_dir = aaka_config.CALENDAR_DIR
    with tempfile.TemporaryDirectory() as td:
        aaka_config.CALENDAR_DIR = Path(td)
        try:
            (Path(td) / "today.md").write_text("# Today's Schedule\nfamily-wide")
            check("admin with no per-member file falls back to today.md",
                  aaka_config.member_calendar_file("today", admin["id"]) == Path(td) / "today.md")
            check("scoped member with no per-member file gets None (never today.md)",
                  aaka_config.member_calendar_file("today", kid["id"]) is None)
            (Path(td) / f"today_{kid['id']}.md").write_text("# Today — kid\n_No events found._")
            check("scoped member's own file wins once it exists",
                  aaka_config.member_calendar_file("today", kid["id"]) == Path(td) / f"today_{kid['id']}.md")
        finally:
            aaka_config.CALENDAR_DIR = real_dir

    # ── scheduled pushes: empty member view never becomes the family view ──
    import sensor.scheduled_summaries as ss
    check("push: admin with empty own text gets the group text",
          ss._member_view(admin["id"], "", "GROUP", "EMPTY") == "GROUP")
    check("push: scoped member with empty own text gets the empty line, not the group",
          ss._member_view(kid["id"], "", "GROUP", "EMPTY") == "EMPTY")
    check("push: own text always wins",
          ss._member_view(kid["id"], "MINE", "GROUP", "EMPTY") == "MINE")

    # ── morning task block: owner filter ───────────────────────────────────
    from skills.tasks import local_tasks as lt
    real_load = lt._load
    today = lt._today()
    lt._load = lambda: [
        {"id": "1", "title": "Parent errand", "status": "open", "due_date": today, "owner": admin["id"]},
        {"id": "2", "title": "Kid chore", "status": "open", "due_date": today, "owner": kid["id"]},
        {"id": "3", "title": "Unowned", "status": "open", "due_date": today, "owner": ""},
    ]
    try:
        fam = lt.summary_for_push()
        mine = lt.summary_for_push(owner=kid["id"])
        check("family task push lists everything", "Parent errand" in fam and "Kid chore" in fam)
        check("owner-filtered push drops other members' tasks", "Parent errand" not in mine)
        check("owner-filtered push keeps own + unowned tasks", "Kid chore" in mine and "Unowned" in mine)
    finally:
        lt._load = real_load

    # ── sidecar writer end to end (skipped if the Google client libs are absent) ──
    try:
        from skills.calendar import sidecar_sync as sc
    except ImportError as e:  # pragma: no cover - sensor without google libs
        print(f"  skip sidecar writer ({e.name} not installed)")
        sc = None
    if sc is not None:
        import datetime as _dt
        fam_cal = aaka_config.calendar_id()
        day = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
        def gev(i, summary):
            return {"id": f"e{i}", "summary": summary, "_cal_id": fam_cal, "_emoji": "",
                    "_private_member": "",
                    "start": {"dateTime": f"{day}T1{i}:00:00Z"},
                    "end": {"dateTime": f"{day}T1{i}:30:00Z"}}
        events = [gev(0, f"{admin['name']} @ Dentist"), gev(1, f"{name} @ Football"),
                  gev(2, "Plumber"), gev(3, f"{household} @ Trip")]
        real_cal = sc.CALENDAR
        with tempfile.TemporaryDirectory() as td:
            sc.CALENDAR = Path(td)
            try:
                sc.write_member_md_files(events)
                kid_md = (Path(td) / f"weekly_{kid['id']}.md").read_text()
                adm_md = (Path(td) / f"weekly_{admin['id']}.md").read_text()
            finally:
                sc.CALENDAR = real_cal
        check("sync: kid's weekly file has their football", "Football" in kid_md)
        check("sync: kid's weekly file has the household trip", "Trip" in kid_md)
        check("sync: kid's weekly file has NO dentist / plumber",
              "Dentist" not in kid_md and "Plumber" not in kid_md)
        check("sync: admin's weekly file has all four",
              all(w in adm_md for w in ("Dentist", "Football", "Plumber", "Trip")))

    # ── parents-only blocks in a member's brief ────────────────────────────
    # A kid got the family's birthday list in their 07:00 push (2026-09-22).
    # Birthdays + the fix-issue merge are admin work; a scoped member's brief
    # is just their day.
    import sensor.router_sensor as rs
    real_dir2 = aaka_config.CALENDAR_DIR
    with tempfile.TemporaryDirectory() as td:
        aaka_config.CALENDAR_DIR = Path(td)
        rs.aaka_config.CALENDAR_DIR = Path(td)
        try:
            for who in (admin["id"], kid["id"]):
                (Path(td) / f"today_{who}.md").write_text(
                    "# Today\n_Last synced: now_\n**Monday** 1 January 2026\n"
                    "🏠**1730** Taekwondo (1h)")
            calls = {"fix": [], "bday": []}

            def fake_analyze_fix(period, member_id=None):
                calls["fix"].append(member_id)
                return [{"kind": "carrier", "title": "X", "date": "2026-01-01"}]
            import skills.calendar.fix_analyzer as fx
            real_fix = fx.analyze_fix
            fx.analyze_fix = fake_analyze_fix

            import skills.contacts.birthday_list as bl
            real_window = bl._load_window

            def fake_window():
                calls["bday"].append(1)
                return []
            bl._load_window = fake_window
            try:
                admin_view = rs._build_today_schedule(admin["id"])
                n_admin_fix, n_admin_bday = len(calls["fix"]), len(calls["bday"])
                kid_view = rs._build_today_schedule(kid["id"])
                check("brief: admin's view runs the fix analyzer", n_admin_fix == 1)
                check("brief: admin's view reads the birthday book", n_admin_bday == 1)
                check("brief: a scoped member's view never touches the fix analyzer",
                      len(calls["fix"]) == n_admin_fix)
                check("brief: …nor the birthday book", len(calls["bday"]) == n_admin_bday)
                check("brief: the scoped member still gets their schedule",
                      "Taekwondo" in kid_view and "Taekwondo" in admin_view)
            finally:
                fx.analyze_fix = real_fix
                bl._load_window = real_window
        finally:
            aaka_config.CALENDAR_DIR = real_dir2
            rs.aaka_config.CALENDAR_DIR = real_dir2

    if _FAILURES:
        print(f"\n{len(_FAILURES)} failure(s)")
        sys.exit(1)
    print("\nall passed")


if __name__ == "__main__":
    main()
