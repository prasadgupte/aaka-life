"""
Aaka — Weekly Digest (Sunday 18:00)

Summarises the past week and previews the next.
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config
from skills.engagement.templates import WEEKLY_DIGEST


def build_weekly_digest(member_id: str) -> str | None:
    m_obj = aaka_config.member_by_name(member_id) or {}
    name = m_obj.get("name", member_id.capitalize())

    # Count this week's events from weekly cache
    CALENDAR = aaka_config.CALENDAR_DIR
    cache_path = CALENDAR / "weekly_events.json"
    event_count = 0
    carrier_issues = 0
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    sunday = monday + datetime.timedelta(days=6)

    if cache_path.exists():
        try:
            events = json.loads(cache_path.read_text())
            for ev in events:
                d = ev.get("date", "")
                if str(monday) <= d <= str(sunday):
                    event_count += 1
                    if "❓" in ev.get("title", "") or ev.get("carriers") == ["❓"]:
                        carrier_issues += 1
        except Exception:
            pass

    # Next week preview: count events
    next_monday = monday + datetime.timedelta(days=7)
    next_sunday = next_monday + datetime.timedelta(days=6)
    next_count = 0
    next_carriers = 0
    if cache_path.exists():
        try:
            events = json.loads(cache_path.read_text())
            for ev in events:
                d = ev.get("date", "")
                if str(next_monday) <= d <= str(next_sunday):
                    next_count += 1
                    if "❓" in ev.get("title", "") or ev.get("carriers") == ["❓"]:
                        next_carriers += 1
        except Exception:
            pass

    summary_parts = [f"{event_count} events this week"]
    if carrier_issues:
        summary_parts.append(f"{carrier_issues} carrier gap{'s' if carrier_issues != 1 else ''}")

    next_week_line = f"{next_count} events scheduled"
    if next_carriers:
        next_week_line += f", {next_carriers} need carriers — reply w #fix"
    else:
        next_week_line += " — reply w for details"

    return WEEKLY_DIGEST.format(
        name=name,
        summary=", ".join(summary_parts),
        next_week_line=next_week_line,
    )
