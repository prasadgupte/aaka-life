"""
Aaka — per-member calendar scope.

Which family-calendar events a member's own views (today_<m>.md, weekly_<m>.md,
member_events.json, the 07:00 / 21:00 / weekend pushes) may carry.

Admins see the whole family calendar. Everyone else — a kid, a grandparent —
sees only what concerns them: an event that names them in its title or
description, lists their email as an attendee, or is addressed to the whole
household. Their own calendars (personal/work/…) always count as theirs.
`aaka_config.member_calendar_scope(id)` decides which mode applies;
`calendar_scope: all|mine` in aaka.yaml overrides the default per member.

Pure module — no Google, no filesystem — so the matching rule is unit-testable
on both the sensor and the executor.
"""
from __future__ import annotations

import re

import aaka_config


def _mentions(text: str, name: str) -> bool:
    """Word-bounded, case-insensitive name match — the same rule
    `analyze_event_title` uses, so an event that renders *Ari* is one that
    is scoped to him."""
    if not name or not text:
        return False
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.I) is not None


def _attendee_emails(ev: dict) -> set[str]:
    out = set()
    for att in ev.get("attendees") or []:
        if isinstance(att, dict) and att.get("email"):
            out.add(att["email"].strip().lower())
    return out


def event_involves_member(ev: dict, member: dict, *, household_name: str = "") -> bool:
    """True if a family-calendar event belongs in this member's own view.

    `ev` is a raw Google event (summary/description/attendees). Matches when:
      - the member's name appears in the title or description, or
      - the member's email is on the attendee list, or
      - the event is addressed to the household pseudo-member (a whole-family
        event — a trip, a public holiday entered by hand, "<Household> @ …").
    Untitled events without any of those are nobody's.
    """
    name = (member.get("name") or "").strip()
    text = f"{ev.get('summary') or ''}\n{ev.get('description') or ''}"
    if _mentions(text, name):
        return True
    email = (member.get("email") or "").strip().lower()
    if email and email in _attendee_emails(ev):
        return True
    if household_name and _mentions(ev.get("summary") or "", household_name):
        return True
    return False


def members_for_event(ev: dict, cal_members: list[str], own_cal_members: list[str],
                      *, roster: dict[str, dict] | None = None,
                      household_name: str | None = None) -> list[str]:
    """Filter the members a calendar feeds (`cal_members`) down to those who may
    see this particular event. Members in `own_cal_members` (the calendar is
    one of theirs) and members whose scope is 'all' always pass; the rest need
    `event_involves_member`."""
    roster = roster if roster is not None else {m["id"]: m for m in aaka_config.members()}
    household = aaka_config.group_name() if household_name is None else household_name
    out = []
    for mid in cal_members:
        if mid in own_cal_members or aaka_config.member_calendar_scope(mid) == "all":
            out.append(mid)
            continue
        m = roster.get(mid)
        if m and event_involves_member(ev, m, household_name=household):
            out.append(mid)
    return out
