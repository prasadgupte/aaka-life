"""
Aaka — Morning Brief Nudge

Reads today_{member}.md (or today.md fallback) and sends a compact schedule
summary at 07:00–08:00 local time. Max 1 per day per member.
"""

import datetime
import os
import re
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config
from skills.engagement.templates import (
    MORNING_BRIEF, MORNING_BRIEF_HINT_CONFLICT, MORNING_BRIEF_HINT_CARRIER,
    MORNING_BRIEF_HINT_QUIET, MORNING_NO_EVENTS,
)


def _reminders_block(raw: str, member_id: str) -> str:
    """Contextual reminders for today, appended to the brief.

    A reminder must reach you on the morning it applies, so it belongs in the
    push, not only in the pulled /today view. Best-effort: a broken rule must
    never cost you the brief itself.
    """
    try:
        from skills.reminders.rules import render as _render
        return _render(raw, member=member_id)
    except Exception:
        return ""


def build_morning_brief(member_id: str) -> str | None:
    """Return the morning brief message text, or None if nothing to send."""
    CALENDAR = aaka_config.CALENDAR_DIR
    path = CALENDAR / f"today_{member_id}.md"
    if not path.exists():
        path = CALENDAR / "today.md"
    if not path.exists():
        return None

    raw = path.read_text()
    m_obj = aaka_config.member_by_name(member_id) or {}
    name = m_obj.get("name", member_id.capitalize())

    try:
        tz = aaka_config.timezone()
        import zoneinfo
        now = datetime.datetime.now(zoneinfo.ZoneInfo(tz))
    except Exception:
        now = datetime.datetime.now()
    date_str = now.strftime("%-d %b %Y")

    # Count timed events (lines with **HHMM**)
    events = [l for l in raw.splitlines() if re.search(r'\*\*\d{4}\*\*', l)]
    event_count = len(events)

    if event_count == 0:
        # A free day still has reminders — "no events" is exactly when a
        # standing routine is easiest to forget.
        return MORNING_NO_EVENTS.format(name=name, date=date_str) + \
            _reminders_block(raw, member_id)

    # Build schedule_line: first 2 events condensed
    schedule_lines = []
    for line in events[:2]:
        clean = re.sub(r'\*\*(\d{4})\*\*', r'\1', line).strip()
        clean = re.sub(r'\*([^*]+)\*', r'\1', clean)  # remove bold
        schedule_lines.append(clean[:60])
    if event_count > 2:
        schedule_lines.append(f"+ {event_count - 2} more")
    schedule_line = "\n".join(schedule_lines)

    # Hint: prefer conflict > carrier > generic
    hint = MORNING_BRIEF_HINT_QUIET
    if "🧱" in raw or "🔀" in raw or ("#fix" in raw.lower()):
        # Try to extract a conflict time
        conflict_time = ""
        m = re.search(r'\*\*(\d{2})(\d{2})\*\*', raw)
        if m:
            conflict_time = f"{m.group(1)}:{m.group(2)}"
        hint = MORNING_BRIEF_HINT_CONFLICT.format(time=conflict_time or "?")
    elif "🤝 ❓" in raw or "❓" in raw:
        attendee = ""
        m = re.search(r'\*([A-Z][a-z]+)\*.*?🤝\s*❓', raw)
        if m:
            attendee = m.group(1)
        hint = MORNING_BRIEF_HINT_CARRIER.format(attendee=attendee or "someone")

    return MORNING_BRIEF.format(
        name=name,
        date=date_str,
        schedule_line=schedule_line,
        hint=hint,
    ) + _reminders_block(raw, member_id)
