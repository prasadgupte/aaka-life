#!/usr/bin/env python3
"""
Aaka — /plan slot-finder.
Zero-token NL parser → free/reserved slot output.
"""

import re, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import os, sys
BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config
from skills.calendar import availability


# ── Keyword tables ────────────────────────────────────────────────────────────

_WEEKDAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]

_WINDOW_MAP = {
    "morning":   ("08:00", "12:00"),
    "afternoon": ("14:00", "18:00"),
    "evening":   ("18:00", "22:00"),
    "lunch":     ("12:00", "14:00"),
    "dinner":    ("18:00", "23:00"),
}

_DURATION_MAP = {
    "lunch":  60,
    "dinner": 180,
}

_IGNORE_RESERVED_MAP = {
    "lunch": ["lunch sync"],
}

_WORK_WINDOW = ("09:00", "16:30")
_DEFAULT_WINDOW = ("09:00", "18:00")
_DEFAULT_DURATION = 30


def _next_weekday(name: str, from_date: datetime.date | None = None) -> datetime.date:
    """Return the next occurrence of a weekday (today counts if it matches)."""
    today = from_date or datetime.date.today()
    target = _WEEKDAY_NAMES.index(name)
    delta = (target - today.weekday()) % 7
    return today + datetime.timedelta(days=delta or 7)


def _intersect_window(w1: tuple[str, str], w2: tuple[str, str]) -> tuple[str, str]:
    """Return intersection of two HH:MM time windows, or the tighter if disjoint."""
    start = max(w1[0], w2[0])
    end   = min(w1[1], w2[1])
    if start >= end:
        return w2  # fallback: use w2 (work window)
    return (start, end)


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_plan_args(text: str) -> dict:
    """
    Zero-token regex parser for /plan arguments.
    Returns dict with keys: dates, duration_minutes, time_window,
    use_work_hours, ignore_reserved, tags, recap, window_kw.
    """
    s = text.lower().strip()
    today = datetime.date.today()

    # ── Duration override (e.g. "2h", "45min", "90m") ──
    dur_m = re.search(r'(\d+)\s*h(?:our)?(?:s)?\b', s)
    dur_min = re.search(r'(\d+)\s*min(?:ute)?(?:s)?\b', s)
    dur_monly = re.search(r'(\d+)\s*m\b', s)

    # ── Window keyword ──
    window_kw = None
    for kw in _WINDOW_MAP:
        if re.search(rf'\b{kw}\b', s):
            window_kw = kw
            break

    # Duration
    if dur_m:
        duration_minutes = int(dur_m.group(1)) * 60
        if dur_min:
            duration_minutes += int(dur_min.group(1))
    elif dur_min:
        duration_minutes = int(dur_min.group(1))
    elif dur_monly:
        duration_minutes = int(dur_monly.group(1))
    else:
        duration_minutes = _DURATION_MAP.get(window_kw, _DEFAULT_DURATION)

    # Time window
    time_window = _WINDOW_MAP.get(window_kw, _DEFAULT_WINDOW)

    # Ignore-reserved
    ignore_reserved = _IGNORE_RESERVED_MAP.get(window_kw, [])

    # Work hours context
    use_work_hours = bool(re.search(r'#work\b|\bwork\b', s))

    # Tags
    tags = re.findall(r'#(\w+)', text)

    # work_days_only: weekday-oriented windows + #work keyword; cleared by weekend keyword
    work_days_only = use_work_hours or window_kw in ("morning", "lunch", "afternoon")
    if re.search(r'\bweekends?\b', s):
        work_days_only = False

    # ── Timeframe ──
    dates: list[datetime.date] = []
    timeframe_label = ""
    day_filter_label = ""

    if re.search(r'\btoday\b', s):
        dates = [today]
        timeframe_label = today.strftime("%-d-%b")
    elif re.search(r'\btomorrow\b', s):
        d = today + datetime.timedelta(days=1)
        dates = [d]
        timeframe_label = d.strftime("%-d-%b")
    elif re.search(r'\bnext\s+week\b', s):
        days_to_mon = (7 - today.weekday()) % 7 or 7
        next_mon = today + datetime.timedelta(days=days_to_mon)
        dates = [next_mon + datetime.timedelta(days=i) for i in range(5)]
        timeframe_label = "next week"
        day_filter_label = "weekdays"
    elif re.search(r'\bthis\s+week\b', s):
        monday = today - datetime.timedelta(days=today.weekday())
        dates = [monday + datetime.timedelta(days=i) for i in range(5)
                 if monday + datetime.timedelta(days=i) >= today]
        timeframe_label = "this week"
        day_filter_label = "weekdays"
    elif re.search(r'\bnext\s+fortnight\b', s):
        dates = [today + datetime.timedelta(days=i) for i in range(15, 29)]
        timeframe_label = "next fortnight"
    elif re.search(r'\bfortnight\b', s):
        dates = [today + datetime.timedelta(days=i) for i in range(1, 15)]
        timeframe_label = "fortnight"
    elif re.search(r'\bnext\s+month\b', s):
        dates = [today + datetime.timedelta(days=i) for i in range(31, 61)]
        timeframe_label = "next month"
    elif re.search(r'\bmonth\b', s):
        dates = [today + datetime.timedelta(days=i) for i in range(1, 31)]
        timeframe_label = "month"
    elif re.search(r'\bweekends?\b', s):
        plural = re.search(r'\bweekends\b', s)
        sat = today + datetime.timedelta(days=(5 - today.weekday()) % 7 or 7)
        if plural:
            dates = []
            for i in range(4):
                s_date = sat + datetime.timedelta(weeks=i)
                dates.extend([s_date, s_date + datetime.timedelta(days=1)])
            timeframe_label = "weekends"
        else:
            dates = [sat, sat + datetime.timedelta(days=1)]
            timeframe_label = "this weekend"
            day_filter_label = "weekends"
    else:
        # Check for explicit date: DD-Mon or DD Mon (e.g. "03-jun", "3 jun", "jun 3")
        explicit_date = None
        _month_names = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"]
        dm = re.search(r'(\d{1,2})[-\s]([a-z]{3})\b', s)
        md = re.search(r'([a-z]{3})\s+(\d{1,2})\b', s)
        match_obj = dm or md
        if match_obj:
            if dm:
                day_s, mon_s = dm.group(1), dm.group(2)
            else:
                mon_s, day_s = md.group(1), md.group(2)
            if mon_s in _month_names:
                mon_num = _month_names.index(mon_s) + 1
                day_num = int(day_s)
                candidate = datetime.date(today.year, mon_num, day_num)
                if candidate < today:
                    candidate = datetime.date(today.year + 1, mon_num, day_num)
                explicit_date = candidate
        if explicit_date:
            dates = [explicit_date]
            timeframe_label = explicit_date.strftime("%-d-%b")
        else:
            # Check for plural weekday (e.g. "fridays", "mondays")
            plural_match = None
            for wd in _WEEKDAY_NAMES:
                if re.search(rf'\b{wd}s\b', s):
                    plural_match = wd
                    break

            if plural_match:
                first = _next_weekday(plural_match)
                dates = [first + datetime.timedelta(weeks=i) for i in range(4)]
                timeframe_label = plural_match.capitalize() + "s"
            else:
                # Singular weekday
                for wd in _WEEKDAY_NAMES:
                    if re.search(rf'\b{wd}\b', s):
                        d = _next_weekday(wd)
                        dates = [d]
                        timeframe_label = d.strftime("%a %-d-%b")
                        break

    if not dates:
        dates = [today]
        if not timeframe_label:
            timeframe_label = today.strftime("%-d-%b")

    # Apply work_days_only filter
    if work_days_only:
        weekday_dates = [d for d in dates if d.weekday() < 5]
        if weekday_dates:
            dates = weekday_dates
            if use_work_hours:
                day_filter_label = "workdays"
            elif not day_filter_label:
                day_filter_label = "weekdays"
        else:
            # e.g. today is Sunday → jump to next Monday
            days_ahead = (7 - today.weekday()) % 7 or 7
            next_mon = today + datetime.timedelta(days=days_ahead)
            dates = [next_mon]
            timeframe_label = next_mon.strftime("%a %-d-%b")
            day_filter_label = "workdays" if use_work_hours else "weekdays"

    # Build recap
    if duration_minutes < 60:
        dur_str = f"{duration_minutes}min"
    elif duration_minutes % 60 == 0:
        dur_str = f"{duration_minutes // 60}h"
    else:
        dur_str = f"{duration_minutes // 60}h {duration_minutes % 60}m"
    recap_parts = [p for p in [timeframe_label, day_filter_label, window_kw, dur_str] if p]
    recap = " · ".join(recap_parts)

    return {
        "dates": dates,
        "duration_minutes": duration_minutes,
        "time_window": time_window,
        "use_work_hours": use_work_hours,
        "ignore_reserved": ignore_reserved,
        "tags": tags,
        "recap": recap,
        "window_kw": window_kw,
    }


# ── Slot finder ───────────────────────────────────────────────────────────────

def find_plan_slots(
    dates: list[datetime.date],
    duration_minutes: int,
    time_window: tuple[str, str],
    use_work_hours: bool,
    ignore_reserved: list[str],
    tags: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Find available and reserved slots across the given dates.
    Returns (available_slots, reserved_slots).
    Each slot: {"start": datetime, "end": datetime}.
    """
    tz_str = aaka_config.timezone()
    tz = ZoneInfo(tz_str)

    # Effective window
    effective_window = time_window
    if use_work_hours:
        effective_window = _intersect_window(time_window, _WORK_WINDOW)

    # For plural weekday searches, find_slots ranges over multiple weeks
    start_date = min(dates)
    end_date   = max(dates)

    # allowed_days: lowercase weekday names for the given dates
    allowed_days: set[str] | None = None
    unique_weekdays = {d.strftime("%A").lower() for d in dates}
    if len(unique_weekdays) < 7:
        allowed_days = unique_weekdays

    cal_ids = aaka_config.all_calendar_ids()

    raw_slots = availability.find_slots(
        cal_ids,
        start_date=start_date,
        end_date=end_date,
        duration_minutes=duration_minutes,
        time_window=effective_window,
        allowed_days=allowed_days,
        timezone=tz_str,
    )

    # Thin: keep first slot per 1h block per day to avoid showing 8 x 30-min slots
    thinned: list[dict] = []
    last_block: dict[str, datetime.datetime] = {}  # date → last slot start (block anchor)
    for slot in raw_slots:
        local_s = slot["start"].astimezone(tz)
        day_key = local_s.strftime("%Y-%m-%d")
        anchor = last_block.get(day_key)
        if anchor is None or (local_s - anchor).total_seconds() >= 3600:
            thinned.append(slot)
            last_block[day_key] = local_s

    # Load reserved blocks
    reserved_blocks = aaka_config.reserved_hours()

    def _overlaps_reserved(slot: dict) -> str | None:
        """Return label if slot overlaps a reserved block, else None."""
        local_s = slot["start"].astimezone(tz)
        local_e = slot["end"].astimezone(tz)
        day_abbr = local_s.strftime("%a")  # e.g. "Mon"
        slot_start_hm = local_s.strftime("%H:%M")
        slot_end_hm   = local_e.strftime("%H:%M")
        for block in reserved_blocks:
            label = block.get("label", "")
            if label in ignore_reserved:
                continue
            if day_abbr not in block.get("days", []):
                continue
            b_start = block.get("start", "")
            b_end   = block.get("end", "")
            # Overlap: slot_start < b_end AND slot_end > b_start
            if slot_start_hm < b_end and slot_end_hm > b_start:
                return label
        return None

    available: list[dict] = []
    reserved: list[dict]  = []
    for slot in thinned:
        label = _overlaps_reserved(slot)
        if label:
            slot["_reserved_label"] = label
            reserved.append(slot)
        else:
            available.append(slot)

    return available[:5], reserved[:3]


# ── Reply formatter ───────────────────────────────────────────────────────────

def parse_day_date(text: str) -> "datetime.date":
    """
    Parse a single target date from /day <text>.
    Handles: blank/today, tomorrow, weekday names, DD-Mon format.
    Returns datetime.date (today if unrecognised).
    """
    s = text.lower().strip()
    today = datetime.date.today()
    if not s or s == "today":
        return today
    if s == "tomorrow":
        return today + datetime.timedelta(days=1)
    # DD-Mon or Mon-DD or "Jun 3" etc.
    _month_names = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"]
    dm = re.search(r'(\d{1,2})[-\s]([a-z]{3})\b', s)
    md = re.search(r'([a-z]{3})\s+(\d{1,2})\b', s)
    match_obj = dm or md
    if match_obj:
        if dm:
            day_s, mon_s = dm.group(1), dm.group(2)
        else:
            mon_s, day_s = md.group(1), md.group(2)
        if mon_s in _month_names:
            mon_num = _month_names.index(mon_s) + 1
            day_num = int(day_s)
            candidate = datetime.date(today.year, mon_num, day_num)
            if candidate < today:
                candidate = datetime.date(today.year + 1, mon_num, day_num)
            return candidate
    # Weekday name
    for wd in _WEEKDAY_NAMES:
        if re.search(rf'\b{wd}\b', s):
            return _next_weekday(wd)
    return today


def describe() -> str:
    return (
        "/plan — find free slots in the family calendar\n\n"
        "Usage: /plan [timeframe] [window] [duration]\n\n"
        "Timeframes:\n"
        "  today, tomorrow, this week, next week, friday, fridays, weekend, fortnight\n\n"
        "Windows:\n"
        "  morning (08–12)  afternoon (14–18)  evening (18–22)\n"
        "  lunch (12–14)    dinner (18–23)\n\n"
        "Duration:\n"
        "  1h, 2h, 45min, 90m  (default: 30min)\n\n"
        "Examples:\n"
        "  /plan friday afternoon 1h\n"
        "  /plan this week morning\n"
        "  /plan tomorrow 2h\n"
        "  /plan weekends dinner"
    )


def format_plan_reply(
    dates: list[datetime.date],
    available: list[dict],
    reserved: list[dict],
    duration_minutes: int,
    recap: str = "",
) -> str:
    tz_str = aaka_config.timezone()
    tz = ZoneInfo(tz_str)

    # Duration label
    if duration_minutes < 60:
        dur_label = f"{duration_minutes} min"
    elif duration_minutes % 60 == 0:
        dur_label = f"{duration_minutes // 60}h"
    else:
        dur_label = f"{duration_minutes // 60}h {duration_minutes % 60}m"

    def _fmt_slot(slot: dict) -> str:
        s = slot["start"].astimezone(tz)
        return f"{s.strftime('%a %-d-%b %H%M')} ({dur_label})"

    lines = []
    if recap:
        lines.append(f"🔍 {recap}")
        lines.append("")

    if available:
        lines.append("Available:")
        for i, slot in enumerate(available, 1):
            lines.append(f"  {i}. {_fmt_slot(slot)}")
    else:
        lines.append("No free slots found.")

    if reserved:
        lines.append("")
        lines.append("Reserved (less preferred):")
        offset = len(available)
        for i, slot in enumerate(reserved, offset + 1):
            label = slot.get("_reserved_label", "")
            lines.append(f"  {i}. {_fmt_slot(slot)}  [{label}]")

    return "\n".join(lines)
