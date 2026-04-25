"""
Aaka — Birthday List

Reads birthday_window.json (privacy-minimal, sensor-accessible) and
the full birthdays.json (Mac-only, for wish links with phone numbers).

Public API:
  query(arg, sender_id) → str   — format a birthday list or wish link
"""

import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config

CONTACTS_DIR   = aaka_config.DATA_DIR / "contacts"
WINDOW_JSON    = CONTACTS_DIR / "birthday_window.json"
FULL_JSON      = CONTACTS_DIR / "birthdays.json"
BDAY_LISTS_DIR = CONTACTS_DIR / "bday_lists"   # one file per sender, no shared state

_LIST_TTL_HOURS = 24  # bday N expires after this many hours


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_window() -> list[dict]:
    if not WINDOW_JSON.exists():
        return []
    try:
        return json.loads(WINDOW_JSON.read_text())
    except Exception:
        return []


def _load_full() -> list[dict]:
    if not FULL_JSON.exists():
        return []
    try:
        return json.loads(FULL_JSON.read_text())
    except Exception:
        return []


def _find_full(name: str) -> dict | None:
    """Look up a contact in the full cache by exact name."""
    for e in _load_full():
        if e["name"].lower() == name.lower():
            return e
    return None


# ── Date parsing ──────────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_arg(arg: str) -> date | None:
    """
    Parse user date arg to a date object.
    Handles: 'today', 'tomorrow', 'week' (returns None — means 7-day range),
    '13-jun', '13 jun', '27 Apr', '27-Apr-2026'.
    Returns None if not a specific date (i.e. 'week').
    """
    arg = arg.strip().lower()
    today = date.today()
    if arg in ("today", ""):
        return today
    if arg == "tomorrow":
        return today + timedelta(days=1)
    if arg == "week":
        return None  # caller handles as range

    # Try DD-Mon or DD Mon patterns
    m = re.match(r"(\d{1,2})[\s\-]([a-z]{3})", arg)
    if m:
        day = int(m.group(1))
        month = _MONTH_MAP.get(m.group(2)[:3])
        if month:
            try:
                # Use this year, or next year if date has passed
                d = date(today.year, month, day)
                if d < today:
                    d = date(today.year + 1, month, day)
                return d
            except ValueError:
                pass

    # Try ISO-ish: MM-DD or YYYY-MM-DD
    m = re.match(r"(\d{1,2})-(\d{1,2})$", arg)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d < today:
                d = date(today.year + 1, int(m.group(1)), int(m.group(2)))
            return d
        except ValueError:
            pass

    return None


def _md_to_date(md: str) -> date:
    """Convert MM-DD string to date (this year or next)."""
    today = date.today()
    month, day = int(md[:2]), int(md[3:])
    try:
        d = date(today.year, month, day)
    except ValueError:
        d = date(today.year + 1, month, day)
    if d < today:
        try:
            d = date(today.year + 1, month, day)
        except ValueError:
            pass
    return d


# ── Formatting ────────────────────────────────────────────────────────────────
# Presentation lives in skills/contacts/format.py. These thin shims preserve
# the existing names used elsewhere in the codebase.

from skills.contacts.format import (
    format_date_header as _format_date_header,
    format_entry as _format_entry_base,
    wish_url as _wish_url,
)


def _age_resolver(entry: dict, full: "dict | None") -> "int | None":
    if not (full and full.get("year")):
        return None
    today = date.today()
    md = entry["month_day"]
    bday_this = _md_to_date(md)
    turning = today.year if bday_this >= today else today.year + 1
    return turning - full["year"]


def _format_entry(n: int, entry: dict, full: dict | None = None) -> str:
    return _format_entry_base(n, entry, full, age_year_resolver=_age_resolver)


def _wish_link(entry: dict, full: dict | None) -> str:
    """Backward-compatible alias for the renderer's wish_url."""
    return _wish_url(entry, full)


# ── Saved list (for bday N resolution) ───────────────────────────────────────
# One file per sender — no shared mutable state, no cross-sender leakage.

def _sender_list_path(sender_id: str) -> Path:
    safe = re.sub(r"[^\w@.+\-]", "_", sender_id)[:80]
    return BDAY_LISTS_DIR / f"{safe}.json"


def _save_list(sender_id: str, items: list[dict]) -> None:
    if not sender_id:
        return
    BDAY_LISTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
    _sender_list_path(sender_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _load_item(sender_id: str, num: int) -> dict | None:
    if not sender_id:
        return None
    path = _sender_list_path(sender_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        saved_at = datetime.fromisoformat(data["saved_at"])
        if datetime.now(timezone.utc) - saved_at > timedelta(hours=_LIST_TTL_HOURS):
            path.unlink(missing_ok=True)
            return None
        for item in data.get("items", []):
            if item.get("num") == num:
                return item
    except Exception:
        pass
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def query(arg: str, sender_id: str = "") -> str:
    """
    Handle a birthday query and return formatted response text.

    arg: the part after 'bday' (stripped), e.g. '', 'tomorrow', '13-jun', '1'
    """
    arg = arg.strip()
    today = date.today()

    # ── bday N → wish link ────────────────────────────────────────────────────
    if re.match(r"^\d+$", arg):
        if not sender_id:
            return "⚠️ Cannot resolve wish link — sender unknown."
        num = int(arg)
        item = _load_item(sender_id, num)
        if not item:
            return "No birthday list — send `bday` first (lists expire after 24h)."
        full = _find_full(item["name"])
        link = _wish_link(item, full)
        md = item["month_day"]
        bday_date = _md_to_date(md)
        date_str = bday_date.strftime("%-d %b")

        lines = [f"🎂 {item['name']} — {date_str}"]
        if link:
            lines.append(f"📱 {link}")
        else:
            lines.append("No phone number on record.")
        return "\n".join(lines)

    # ── Determine date range ──────────────────────────────────────────────────
    window = _load_window()
    full_entries = _load_full()
    full_map = {e["name"]: e for e in full_entries}

    if not window and not full_entries:
        return "📭 No birthday data yet. Sync pending."

    if arg.lower() == "week" or arg == "":
        # Default: today + 7 days — use window cache
        days_limit = 7
        target_date = None
        source = window
    elif arg.lower() == "today":
        days_limit = 0
        target_date = today
        source = window
    else:
        target_date = _parse_date_arg(arg)
        days_limit = None  # exact date match
        # For specific date queries, search full cache (window only covers 30 days)
        if target_date is not None and full_entries:
            source = [
                {
                    "name":       e["name"],
                    "first_name": e["first_name"],
                    "month_day":  e["birthday"][5:] if len(e["birthday"]) == 10 else e["birthday"],
                    "mobile":     e["mobile"],
                    "email":      e.get("email", ""),
                }
                for e in full_entries
            ]
        else:
            source = window

    # Filter entries
    matched: list[dict] = []
    for entry in source:
        md = entry.get("month_day") or ""
        if not md or len(md) < 5:
            continue
        bday_date = _md_to_date(md)
        days_away = (bday_date - today).days

        if target_date is not None and days_limit is None:
            if bday_date != target_date:
                continue
        elif days_limit is not None:
            if not (0 <= days_away <= days_limit):
                continue
        matched.append({**entry, "_date": bday_date, "_days": days_away})

    if not matched:
        if target_date:
            return f"🎂 No birthdays on {target_date.strftime('%-d %b')}."
        return "🎂 No birthdays in the next 7 days."

    # Sort by date
    matched.sort(key=lambda x: x["_date"])

    # Group by date, build numbered list
    lines = []
    current_date = None
    n = 0
    saved_items = []
    phones_missing = []

    for entry in matched:
        if entry["_date"] != current_date:
            if lines:
                lines.append("")
            current_date = entry["_date"]
            lines.append(_format_date_header(current_date))

        n += 1
        full = full_map.get(entry["name"])
        lines.append(_format_entry(n, entry, full))
        saved_items.append({"num": n, "name": entry["name"], "first_name": entry["first_name"],
                            "month_day": entry["month_day"], "mobile": entry.get("mobile", ""),
                            "email": entry.get("email", "")})
        if not entry.get("mobile"):
            phones_missing.append(n)

    # Hint line for items with phones
    with_phone = [i for i in saved_items if i["mobile"]]
    if with_phone:
        hint_nums = " / ".join(str(i["num"]) for i in with_phone[:3])
        lines.append(f"\n→ Reply: bday {hint_nums} for wish link")

    # Save for bday N resolution
    if sender_id:
        _save_list(sender_id, saved_items)

    # Strip trailing blank
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)
