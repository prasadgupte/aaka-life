"""
Aaka — Silence Nudge

Sent after 3 days of inactivity. References an upcoming event to add relevance.
"""

import os
import re
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config
from skills.engagement.templates import SILENCE_NUDGE, SILENCE_NUDGE_NO_EVENTS


def build_silence_nudge(member_id: str) -> str | None:
    """Return re-engagement text referencing a near-term event, or None."""
    m_obj = aaka_config.member_by_name(member_id) or {}
    name = m_obj.get("name", member_id.capitalize())

    # Pick next notable event from weekly.md
    path = aaka_config.member_calendar_file("weekly", member_id)

    event_teaser = ""
    if path is not None:
        raw = path.read_text()
        # Find the next timed event with an emoji (more interesting events)
        for line in raw.splitlines():
            if re.search(r'\*\*\d{4}\*\*', line) and not line.startswith("#"):
                clean = re.sub(r'\*\*(\d{2})(\d{2})\*\*', r'\1:\2', line).strip()
                clean = re.sub(r'\*([^*]+)\*', r'\1', clean)
                event_teaser = clean[:80]
                break

    if not event_teaser:
        return SILENCE_NUDGE_NO_EVENTS.format(name=name)

    return SILENCE_NUDGE.format(name=name, event_teaser=event_teaser)
