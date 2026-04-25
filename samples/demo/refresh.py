#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0
"""
samples/demo/refresh.py — regenerate demo calendar + task data with
today's actual dates.

The Ash-Kaa sample family lives at fixed times of day on a fixed weekly
pattern (events keyed by weekday 0..6). This script renders that pattern
against the current calendar week, so:

  • today.md           shows the events for today's weekday
  • today_<member>.md  shows the same, filtered per-member
  • weekly.md          shows Monday-Sunday of this week
  • tasks_cache.json   shifts relative-offset due dates to absolute

Run it standalone:
    python3 samples/demo/refresh.py

Or let executor/webui/server.py run it on startup when --config is the
samples/demo dir.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

DEMO_ROOT = Path(__file__).resolve().parent
DATA = DEMO_ROOT / "data"

# ── Weekly event template — keyed by weekday (Mon=0 .. Sun=6) ────────────────
# Each event: time HHMM, owner (display name), optional carrier (helper),
# title, optional place, duration string, emoji.
WEEK: dict[int, list[dict]] = {
    0: [  # Monday
        {"time": "0830", "owner": "Tsu",  "title": "Site review call", "place": "Work", "dur": "45m", "emoji": "🏗️"},
        {"time": "0900", "owner": "Tsu",  "title": "Sprint planning",  "place": "Work", "dur": "45m", "emoji": "🗳️"},
        {"time": "1030", "owner": "Rumi", "carrier": "Alex", "title": "Pediatrician check-up", "dur": "1h", "emoji": "🏥"},
        {"time": "1200", "owner": "Kiran", "title": "Lunch at home", "dur": "1h", "emoji": "🍱"},
        {"time": "1400", "owner": "Ari",  "carrier": "Taylor", "title": "Soccer practice", "dur": "1h30m", "emoji": "⚽"},
        {"time": "1600", "owner": "Ari",  "carrier": "Taylor", "title": "Piano lesson",    "dur": "1h",    "emoji": "🎹"},
        {"time": "1830", "owner": "Family", "title": "Family dinner", "dur": "1h", "emoji": "🍱"},
    ],
    1: [  # Tuesday
        {"time": "0900", "owner": "Alex", "title": "Quarterly planning", "place": "Work", "dur": "2h", "emoji": "🗳️"},
        {"time": "1500", "owner": "Ari",  "carrier": "Alex", "title": "Guitar lesson", "dur": "1h", "emoji": "🎸"},
        {"time": "1600", "owner": "Rumi", "carrier": "Tsu",  "title": "Swimming lesson", "dur": "45m", "emoji": "🏊"},
        {"time": "1800", "owner": "Rumi", "carrier": "Tsu",  "title": "Art class",       "dur": "1h30m", "emoji": "🎨"},
    ],
    2: [  # Wednesday
        {"time": "1000", "owner": "Tsu",  "title": "Client site visit", "place": "Work", "dur": "3h", "emoji": "🏗️"},
        {"time": "1100", "owner": "Kiran", "carrier": "Alex", "title": "Doctor follow-up", "dur": "1h", "emoji": "🏥"},
        {"time": "1600", "owner": "Ari",  "carrier": "Taylor", "title": "Piano lesson", "dur": "1h", "emoji": "🎹"},
    ],
    3: [  # Thursday
        {"time": "1000", "owner": "Alex", "title": "Sprint review", "place": "Work", "dur": "1h", "emoji": "🗳️"},
        {"time": "1400", "owner": "Tsu",  "title": "Client presentation", "place": "Work", "dur": "2h", "emoji": "🏗️"},
        {"time": "1400", "owner": "Ari",  "carrier": "Taylor", "title": "Soccer practice", "dur": "1h30m", "emoji": "⚽"},
        {"time": "1900", "owner": "Family", "title": "Date night — Alex & Tsu (Taylor with kids)", "dur": "2h", "emoji": "🍽️"},
    ],
    4: [  # Friday
        {"time": "1500", "owner": "Ari", "carrier": "Alex", "title": "Piano recital", "dur": "2h", "emoji": "🎹"},
    ],
    5: [  # Saturday
        {"time": "1000", "owner": "Rumi", "carrier": "Alex", "title": "Swimming",       "dur": "1h", "emoji": "🏊"},
        {"time": "1200", "owner": "Family", "title": "Family grocery run", "dur": "1h", "emoji": "🛒"},
        {"time": "1500", "owner": "Kiran", "carrier": "Alex", "title": "Park walk",     "dur": "2h", "emoji": "🌳"},
    ],
    6: [  # Sunday
        {"time": "1100", "owner": "Family", "title": "Family brunch", "dur": "2h", "emoji": "🧺"},
        {"time": "1500", "owner": "Ari",    "title": "Board game club", "dur": "1h", "emoji": "🎯"},
    ],
}

# Tasks — `due_offset_days` is relative to today (today=0, tomorrow=1, ...).
# Past offsets become overdue tasks.
TASKS: list[dict] = [
    {"id": "demo-task-1", "title": "Sign Rumi's school trip permission slip",
     "due_offset_days": 2, "starred": False, "urgent": True,  "tags": ["#school"]},
    {"id": "demo-task-2", "title": "Book boiler service",
     "due_offset_days": 10, "starred": False, "urgent": False, "tags": ["#home"]},
    {"id": "demo-task-3", "title": "Buy piano recital outfit for Ari",
     "due_offset_days": 4, "starred": True,  "urgent": False, "tags": ["#school", "#shop"]},
    {"id": "demo-task-4", "title": "Renew Kiran's prescription",
     "due_offset_days": 7, "starred": False, "urgent": False, "tags": ["#health"]},
    {"id": "demo-task-5", "title": "Replace front door bulb",
     "due_offset_days": -1, "starred": False, "urgent": False, "tags": ["#home"]},
]

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PER_MEMBER_FILES = {"alex": "Alex", "tsu": "Tsu"}     # who gets a per-member view


# ── Rendering ────────────────────────────────────────────────────────────────
def _fmt_event(e: dict) -> str:
    carrier = f" (🤝 {e['carrier']})" if e.get("carrier") else ""
    place = f" @ {e['place']}" if e.get("place") else ""
    if e["owner"] == "Family":
        # Whole-family events: no owner prefix, no dash separator
        return f"{e['emoji']}**{e['time']}** {e['title']}{place} ({e['dur']})"
    if e["owner"] == "Kiran":
        # Elder family members: shown plain (not italicised), no underline
        return f"{e['emoji']}**{e['time']}** Kiran{carrier} — {e['title']}{place} ({e['dur']})"
    return f"{e['emoji']}**{e['time']}** *{e['owner']}*{carrier} — {e['title']}{place} ({e['dur']})"


def _date_header(d: dt.date) -> str:
    return d.strftime(f"%A %d %B %Y")


def render_today(today: dt.date) -> str:
    events = WEEK[today.weekday()]
    lines = [
        f"# Today's Schedule — {_date_header(today)}",
        f"_Last synced: {today.strftime('%Y-%m-%d')} 09:00_",
        "",
    ]
    lines += [_fmt_event(e) for e in events]
    lines += [
        "",
        f"☀️ Amsterdam 14°C · {len(events)} events today",
    ]
    return "\n".join(lines) + "\n"


def render_today_member(today: dt.date, member_name: str) -> str:
    # Per-member view: skip events whose owner is at @ Work AND is someone else
    # (i.e., don't show your spouse's work calls in your personal today).
    events = [
        e for e in WEEK[today.weekday()]
        if not (e.get("place") == "Work" and e["owner"] != member_name)
    ]
    lines = [
        f"# Today — {member_name} · {_date_header(today)}",
        f"_Last synced: {today.strftime('%Y-%m-%d')} 09:00_",
        "",
    ]
    lines += [_fmt_event(e) for e in events]
    lines += [
        "",
        f"☀️ Amsterdam 14°C · {len(events)} events",
    ]
    return "\n".join(lines) + "\n"


def render_weekly(today: dt.date) -> str:
    # Start of week = Monday of the week containing `today`.
    monday = today - dt.timedelta(days=today.weekday())
    lines = [
        "# This Week's Schedule",
        f"_Last synced: {today.strftime('%Y-%m-%d')} 09:00_",
        "",
    ]
    for i in range(7):
        d = monday + dt.timedelta(days=i)
        lines.append(f"**{WEEKDAY_NAMES[i]}** {d.strftime('%d %B %Y')}")
        for e in WEEK[i]:
            lines.append(_fmt_event(e))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_tasks(today: dt.date) -> str:
    out = []
    for t in TASKS:
        due = today + dt.timedelta(days=t["due_offset_days"])
        out.append({
            "id": t["id"],
            "title": t["title"],
            "status": "needsAction",
            "due_date": due.strftime("%Y-%m-%d"),
            "starred": t["starred"],
            "urgent": t["urgent"],
            "tags": t["tags"],
        })
    return json.dumps(out, ensure_ascii=False, indent=2) + "\n"


# ── Main ─────────────────────────────────────────────────────────────────────
def refresh(today: dt.date | None = None, *, verbose: bool = True) -> dict[str, Path]:
    """Regenerate all demo files. Returns map of {what: path_written}."""
    today = today or dt.date.today()
    written: dict[str, Path] = {}

    cal_dir = DATA / "calendar"
    cal_dir.mkdir(parents=True, exist_ok=True)
    p = cal_dir / "today.md"
    p.write_text(render_today(today), encoding="utf-8")
    written["today.md"] = p

    p = cal_dir / "weekly.md"
    p.write_text(render_weekly(today), encoding="utf-8")
    written["weekly.md"] = p

    for member_id, member_name in PER_MEMBER_FILES.items():
        p = cal_dir / f"today_{member_id}.md"
        p.write_text(render_today_member(today, member_name), encoding="utf-8")
        written[f"today_{member_id}.md"] = p

    tasks_dir = DATA / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    p = tasks_dir / "tasks_cache.json"
    p.write_text(render_tasks(today), encoding="utf-8")
    written["tasks_cache.json"] = p

    if verbose:
        print(f"Refreshed demo data for {today.isoformat()}:")
        for what, path in written.items():
            print(f"  {what:24}  {path.relative_to(DEMO_ROOT.parent.parent)}")
    return written


if __name__ == "__main__":
    refresh()
