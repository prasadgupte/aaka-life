"""
Aaka — Proactive Conflict Alert

Checks fix_analyzer for conflicts/missing-carriers happening tomorrow,
and sends an alert if any are found that haven't been alerted yet.
"""

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config
from skills.engagement.templates import CONFLICT_ALERT

_ALERT_CACHE = aaka_config.CALENDAR_DIR / ".conflict_alert_sent.json"


def _load_sent() -> set:
    if _ALERT_CACHE.exists():
        try:
            return set(json.loads(_ALERT_CACHE.read_text()))
        except Exception:
            pass
    return set()


def _save_sent(sent: set) -> None:
    _ALERT_CACHE.write_text(json.dumps(sorted(sent)))


def _issue_key(issue: dict) -> str:
    raw = f"{issue['date']}:{issue['type']}:{issue.get('title','')}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def build_conflict_alerts(member_id: str) -> list[str]:
    """Return list of alert messages for tomorrow's unresolved issues, or empty list."""
    tomorrow = str(datetime.date.today() + datetime.timedelta(days=1))
    try:
        from skills.calendar.fix_analyzer import analyze_fix
        issues = analyze_fix("today", member_id=member_id)
        # Filter to tomorrow only
        tomorrow_issues = [i for i in issues if i.get("date") == tomorrow]
    except Exception:
        return []

    if not tomorrow_issues:
        return []

    sent = _load_sent()
    m_obj = aaka_config.member_by_name(member_id) or {}
    name = m_obj.get("name", member_id.capitalize())

    messages = []
    newly_sent = set()
    for issue in tomorrow_issues:
        key = _issue_key(issue)
        if key in sent:
            continue  # already alerted
        detail = issue.get("detail", issue.get("title", ""))
        messages.append(CONFLICT_ALERT.format(name=name, detail=detail))
        newly_sent.add(key)

    if newly_sent:
        _save_sent(sent | newly_sent)

    return messages
