"""sensor/intents/calendar.py — Local calendar view handlers (zero queue writes)."""
import re

import aaka_config

HANDLES = frozenset({"today_schedule", "weekly_schedule"})


def handle(intent: str, message: str, sender: str, channel_id: str, source: str) -> str:
    CALENDAR = aaka_config.CALENDAR_DIR

    if intent in ("today_schedule", "weekly_schedule"):
        sender_member = aaka_config.member_by_sender(sender)
        if sender_member is None:
            return "🔒 Calendar access requires a recognised sender."

    if intent == "today_schedule":
        from sensor.router_sensor import (
            _strip_fix_flag, _strip_me_flag, _parse_member_arg,
            _namespace_for_sender, _build_today_schedule,
        )
        message, _fix = _strip_fix_flag(message)
        message, _me = _strip_me_flag(message)
        _all = bool(re.search(r'@all\b', message, re.I))
        mid = _parse_member_arg(message) or _namespace_for_sender(sender)
        if not mid:
            return "🔒 Calendar access requires a recognised sender."
        if _fix:
            from skills.calendar.fix_analyzer import analyze_fix, format_fix_list, save_fix_list
            member_arg = None if _all else mid
            issues = analyze_fix("today", member_id=member_arg)
            save_fix_list(sender, issues)
            return format_fix_list(issues)
        return _build_today_schedule(mid, sender=sender)

    if intent == "weekly_schedule":
        from sensor.router_sensor import (
            _strip_fix_flag, _strip_me_flag, _parse_member_arg,
            _namespace_for_sender,
        )
        message, _fix = _strip_fix_flag(message)
        message, _me = _strip_me_flag(message)
        _all = bool(re.search(r'@all\b', message, re.I))
        mid = _parse_member_arg(message) or _namespace_for_sender(sender)
        if not mid:
            return "🔒 Calendar access requires a recognised sender."
        if _fix:
            from skills.calendar.fix_analyzer import analyze_fix, format_fix_list, save_fix_list
            member_arg = None if _all else mid
            issues = analyze_fix("week", member_id=member_arg)
            save_fix_list(sender, issues)
            return format_fix_list(issues)
        path = CALENDAR / f"weekly_{mid}.md"
        if not path.exists():
            path = CALENDAR / "weekly.md"
        if not path.exists():
            return "📅 No calendar data. Sync pending."
        raw = path.read_text()
        from skills.calendar.fix_analyzer import analyze_fix, merge_fixes_inline, save_fix_list
        member_arg = None if _all else mid
        issues = analyze_fix("week", member_id=member_arg)
        raw = merge_fixes_inline(raw, issues)
        if sender and issues:
            save_fix_list(sender, issues)
        from skills.voice import get_voice
        name = (aaka_config.member_by_sender(sender) or {}).get("name", "")
        from sensor.router_sensor import _analyze_weekly_content
        ctx = {"name": name, **_analyze_weekly_content(raw)}
        reply = get_voice().apply("weekly_schedule", raw, ctx)

        try:
            from skills.tasks.local_tasks import list_open
            from skills.tasks.format import render_task_counts
            counts = render_task_counts(list_open(), period="week")
            if counts:
                reply += "\n\n" + counts
        except Exception:
            pass

        return reply

    return "❓ Unknown calendar intent."
