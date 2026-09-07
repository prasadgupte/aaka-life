"""
skills/reminders/rules.py — recurring contextual reminders for the day view.

A reminder is a NOTE ATTACHED TO A DAY, not a task. "Pack the sport bag" is only
useful on the morning it applies; if it goes unheeded you do not want it accruing
as overdue forever. Tasks stay the single list of things you complete — this is a
rendering rule over the calendar you already have, like the carrier ❓ marker.

Why not recurring tasks (skills/tasks): recurrence there fires only when a task is
COMPLETED (recreate_recurring is called from the /done path), so a week you forget
is a week the series stops — exactly backwards for a routine. Its recurrence is
also a timedelta, so "every Monday" cannot be expressed and "weekly" drifts by
however late you ticked the last one.

Conditions (all optional, ANDed):
    weekday: mon            or [mon, wed]     — plain calendar weekday
    event_matches: "sport"                    — regex over today's events, so the
                                                reminder follows the timetable
                                                rather than a guessed weekday
    unless: {…same keys…}                     — suppression (holidays, cancellations)

Matching is deterministic and costs zero tokens: an LLM helps you AUTHOR a rule
(via the MCP tools), it is never in the path that evaluates one.

Store: $AAKA_CONFIG_DIR/config/reminders.yaml, managed through MCP the same way
tools.yaml is. Keyed by id so a rule can be updated or removed by name.
"""
from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

_WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _config_dir() -> Path:
    return Path(os.environ.get("AAKA_CONFIG_DIR") or Path.home() / ".aaka")


def _path() -> Path:
    return _config_dir() / "config" / "reminders.yaml"


def load() -> dict:
    """The reminder manifest: {id: rule}. Empty when unconfigured."""
    import yaml
    p = _path()
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save(man: dict) -> None:
    import yaml
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(man, default_flow_style=False,
                                  allow_unicode=True, sort_keys=True))
    os.replace(tmp, p)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "reminder"


def add(text: str, member: str = "", weekday: str = "", event_matches: str = "",
        unless_matches: str = "", rid: str = "") -> dict:
    """Add or update a reminder. Returns (id, rule)."""
    if not text.strip():
        raise ValueError("a reminder needs text")
    if not weekday and not event_matches:
        raise ValueError(
            "a reminder needs a condition: weekday=… and/or event_matches=…")
    for w in _split(weekday):
        if w.lower() not in _WEEKDAYS:
            raise ValueError(f"unknown weekday {w!r}")
    for pattern in (event_matches, unless_matches):
        if pattern:
            try:
                re.compile(pattern, re.I)
            except re.error as exc:
                raise ValueError(f"invalid pattern {pattern!r}: {exc}") from exc

    man = load()
    key = rid or _slug(f"{member}-{text}")
    when: dict = {}
    if weekday:
        wd = _split(weekday)
        when["weekday"] = wd[0] if len(wd) == 1 else wd
    if event_matches:
        when["event_matches"] = event_matches
    rule = {"text": text.strip(), "when": when, "enabled": True}
    if member:
        rule["for"] = member
    if unless_matches:
        rule["unless"] = {"event_matches": unless_matches}
    man[key] = rule
    save(man)
    return {"id": key, "rule": rule}


def remove(rid: str) -> bool:
    man = load()
    if rid not in man:
        return False
    del man[rid]
    save(man)
    return True


def set_enabled(rid: str, enabled: bool) -> bool:
    man = load()
    if rid not in man:
        return False
    man[rid]["enabled"] = bool(enabled)
    save(man)
    return True


def _split(value) -> list:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _cond_matches(cond: dict, day_text: str, date: datetime.date) -> bool:
    """True when every key in `cond` holds. An empty condition never matches, so
    a malformed rule stays silent instead of firing every day."""
    if not cond:
        return False
    days = _split(cond.get("weekday"))
    if days:
        wanted = {_WEEKDAYS.get(d.lower()) for d in days}
        if date.weekday() not in wanted:
            return False
    pattern = cond.get("event_matches")
    if pattern:
        try:
            if not re.search(str(pattern), day_text or "", re.I):
                return False
        except re.error:
            return False
    return True


def due(day_text: str, date: "datetime.date | None" = None,
        member: str = "") -> list:
    """Reminder texts firing for `member` on `date`, given that day's rendered
    calendar text. Pure: no I/O beyond reading the manifest, no LLM, no clock
    surprises (the date is passed in)."""
    date = date or datetime.date.today()
    out = []
    for rid, rule in sorted(load().items()):
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        owner = str(rule.get("for") or "").strip()
        if owner and member and owner.lower() != member.lower():
            continue
        if not _cond_matches(rule.get("when") or {}, day_text, date):
            continue
        if _cond_matches(rule.get("unless") or {}, day_text, date):
            continue
        text = str(rule.get("text") or "").strip()
        if text:
            out.append(text)
    return out


def render(day_text: str, date: "datetime.date | None" = None,
           member: str = "") -> str:
    """The block appended to a day view. Empty string when nothing fires, so the
    summary is untouched on ordinary days."""
    items = due(day_text, date=date, member=member)
    if not items:
        return ""
    lines = "\n".join(f"• {t}" for t in items)
    return f"\n*Don't forget*\n{lines}"
