#!/usr/bin/env python3
"""
Aaka — /block work-calendar white-space filler.
Finds free gaps in a work calendar and fills them with structured focus/admin blocks.
"""

import re, datetime, json
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

# ── Constants ─────────────────────────────────────────────────────────────────

WORK_WINDOW = ("09:00", "17:00")
MIN_GAP     = 10     # skip gaps < 10 min
CHUNK_MINS  = 120    # preferred focus block size
MAX_BLOCK   = 180    # never emit a block > 180m as a single focus
LUNCH_HOUR  = 12     # lunch block starts at 12:00
LUNCH_DUR   = 60     # lunch block is 1h (12:00–13:00)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_time(dt: datetime.datetime) -> str:
    """Round ±5 min to nearest hour; display as HHh or HHhMM."""
    mins = dt.hour * 60 + dt.minute
    nearest = round(mins / 60) * 60
    if abs(mins - nearest) <= 5:
        return f"{nearest // 60:02d}h"
    return f"{dt.hour:02d}h{dt.minute:02d}"


def _fmt_duration(mins: int) -> str:
    if mins < 60:
        return f"{mins}m"
    if mins % 60 == 0:
        return f"{mins // 60}h"
    return f"{mins // 60}h{mins % 60}m"


# ── Block-filling logic ───────────────────────────────────────────────────────

def _fill_segment(
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[dict]:
    """Fill a single time segment (no lunch boundary) with chunked blocks."""
    blocks = []
    cursor = start

    while True:
        remaining = int((end - cursor).total_seconds() / 60)
        if remaining < MIN_GAP:
            break

        if remaining <= 15:
            blocks.append({
                "title": "🌤️ Task",
                "start": cursor,
                "end": end,
                "duration_mins": remaining,
            })
            break

        if remaining <= 30:
            blocks.append({
                "title": "🌤️ Email Triage",
                "start": cursor,
                "end": end,
                "duration_mins": remaining,
            })
            break

        if remaining <= MAX_BLOCK:
            blocks.append({
                "title": "🌤️ Focus",
                "start": cursor,
                "end": end,
                "duration_mins": remaining,
            })
            break

        # remaining > MAX_BLOCK: emit CHUNK_MINS focus, 30m Email Triage, continue
        chunk_end = cursor + datetime.timedelta(minutes=CHUNK_MINS)
        blocks.append({
            "title": "🌤️ Focus",
            "start": cursor,
            "end": chunk_end,
            "duration_mins": CHUNK_MINS,
        })
        cursor = chunk_end

        remaining_after = int((end - cursor).total_seconds() / 60)
        if remaining_after >= 40:
            rot_end = cursor + datetime.timedelta(minutes=30)
            if rot_end > end:
                rot_end = end
            blocks.append({
                "title": "🌤️ Email Triage",
                "start": cursor,
                "end": rot_end,
                "duration_mins": 30,
            })
            cursor = rot_end

    return blocks


def _fill_gap(
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[dict]:
    """Split one free gap into named blocks, inserting a lunch block if applicable."""
    blocks = []

    lunch_start = start.replace(hour=LUNCH_HOUR, minute=0, second=0, microsecond=0)
    lunch_end   = start.replace(
        hour=LUNCH_HOUR + LUNCH_DUR // 60,
        minute=LUNCH_DUR % 60,
        second=0, microsecond=0,
    )

    # Lunch split: gap must fully straddle 12:00–13:00
    if start < lunch_start and end > lunch_end:
        pre_mins  = int((lunch_start - start).total_seconds() / 60)
        post_mins = int((end - lunch_end).total_seconds() / 60)
        if pre_mins >= MIN_GAP and post_mins >= MIN_GAP:
            blocks.extend(_fill_segment(start, lunch_start))
            blocks.append({
                "title": "🌤️ Lunch",
                "start": lunch_start,
                "end": lunch_end,
                "duration_mins": LUNCH_DUR,
            })
            blocks.extend(_fill_segment(lunch_end, end))
            return blocks

    blocks.extend(_fill_segment(start, end))
    return blocks


def build_blocks(free_slots: list[dict]) -> list[dict]:
    """
    Takes raw slots from availability.find_slots() and fills each free gap
    with structured blocks.  Consecutive 10-min slots are merged into contiguous
    gaps before filling so the algorithm sees the full free window.
    """
    if not free_slots:
        return []

    # Merge consecutive slots into contiguous free gaps
    gaps: list[dict] = []
    for slot in free_slots:
        if gaps and slot["start"] == gaps[-1]["end"]:
            gaps[-1] = {"start": gaps[-1]["start"], "end": slot["end"]}
        else:
            gaps.append({"start": slot["start"], "end": slot["end"]})

    all_blocks: list[dict] = []
    for gap in gaps:
        gap_mins = int((gap["end"] - gap["start"]).total_seconds() / 60)
        if gap_mins < MIN_GAP:
            continue
        all_blocks.extend(_fill_gap(gap["start"], gap["end"]))

    return all_blocks


# ── Formatting ────────────────────────────────────────────────────────────────

def format_block_preview(blocks: list[dict]) -> str:
    """Group blocks by date and format as a human-readable preview."""
    tz = ZoneInfo(aaka_config.timezone())

    by_date: dict[str, dict] = {}
    for b in blocks:
        local_s = b["start"].astimezone(tz)
        day_key = local_s.strftime("%Y-%m-%d")
        if day_key not in by_date:
            by_date[day_key] = {
                "label": local_s.strftime("%a %d-%b"),
                "blocks": [],
            }
        by_date[day_key]["blocks"].append(b)

    multi_day = len(by_date) > 1
    lines: list[str] = []

    for dk in sorted(by_date):
        entry = by_date[dk]
        if multi_day:
            lines.append(f"📅 {entry['label']}")
        for b in entry["blocks"]:
            local_s = b["start"].astimezone(tz)
            local_e = b["end"].astimezone(tz)
            s_str = _fmt_time(local_s)
            e_str = _fmt_time(local_e)
            dur_str = _fmt_duration(b["duration_mins"])
            lines.append(f"  {s_str}–{e_str}   {b['title']} ({dur_str})")

    return "\n".join(lines)


# ── Executor ──────────────────────────────────────────────────────────────────

def execute_blocks(
    blocks: list[dict],
    calendar_id: str,
    work_email: str,
    tz: str,
) -> str:
    """Create each block on the work calendar. Returns a summary string."""
    import gog
    count = 0
    for b in blocks:
        s = b["start"].strftime("%Y-%m-%dT%H:%M:%S")
        e = b["end"].strftime("%Y-%m-%dT%H:%M:%S")
        guests = [work_email] if work_email else []
        gog.create_event(
            b["title"], s, e,
            timezone=tz,
            calendar_id=calendar_id,
            visibility="private",
            guests=guests,
            dont_notify=True,
        )
        count += 1
    return f"✅ Created {count} blocks."


# ── Argument parser ───────────────────────────────────────────────────────────

def describe() -> str:
    return (
        "/block — fill free gaps in the work calendar with focus/admin blocks\n\n"
        "Usage: /block [timeframe]\n\n"
        "Timeframes:\n"
        "  (empty)     → today + tomorrow\n"
        "  week        → today + next 8 work days\n"
        "  friday      → single named day\n"
        "  next week   → next 5 work days\n\n"
        "On confirm, blocks are written to the work calendar:\n"
        "  🌤️ Focus (2h)  🌤️ Email Triage (30m)  🌤️ Lunch (1h)  🌤️ Task (≤15m)\n\n"
        "Examples:\n"
        "  /block\n"
        "  /block week\n"
        "  /block tomorrow"
    )


def parse_block_args(text: str) -> dict:
    """
    Parse /block command arguments.
    Returns {"dates": list[date], "plan_mode": bool, "recap": str}.
    """
    from skills.calendar import plan as plan_mod

    # Strip /block prefix
    s = re.sub(r"^/block\s*", "", text, flags=re.I).strip()

    # Detect plan mode
    plan_mode = bool(re.search(r"\bplan\b", s, re.I))
    s = re.sub(r"\bplan\b", "", s, flags=re.I).strip()

    today = datetime.date.today()

    # "week" → today + 8 work days
    if re.search(r"\bweek\b", s, re.I):
        dates: list[datetime.date] = []
        d = today
        while len(dates) < 9:  # today + up to 8 more work days
            if d.weekday() < 5:
                dates.append(d)
            d += datetime.timedelta(days=1)
        recap = "this week · workdays"

    elif not s:
        # Default: today + tomorrow
        tomorrow = today + datetime.timedelta(days=1)
        dates = [today, tomorrow]
        recap = "today + tomorrow"

    else:
        args = plan_mod.parse_plan_args(s)
        dates = args["dates"]
        recap = args.get("recap", s)

    return {"dates": dates, "plan_mode": plan_mode, "recap": recap}
