"""
skills/calendar/edit_event.py — `/cal edit` search + apply helpers.

Two-step flow, mirroring the fix_analyzer pattern:

  Step 1: `/cal edit <search>`
    → search_candidates(query, sender_id) returns matching events
    → format_candidate_list() renders a numbered list
    → save_edit_list() persists candidates keyed by sender

  Step 2: `/cal edit <N> <modifiers>`
    → load_edit_item(sender_id, N) retrieves the saved candidate
    → parse_modifiers(text, sender_id) extracts changes
    → apply_changes(candidate, changes, token_file) calls gog.update_event

Modifier grammar (free-form, order-independent, all optional):

  + @alex @tsu <email>       add guests by member tag or literal email
  - @alex                    (not supported — reserved for future)
  time HH:MM-HH:MM           change start/end times (same date)
  time HH:MM                 change start time, keep duration
  date YYYY-MM-DD            change date
  title: <new title>         replace title (until end of line)
  loc: <new location>        replace location
  desc: <new description>    replace description
  private | public           visibility
  no-email                   suppress guest notifications
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config

EDIT_LIST_PATH = aaka_config.CALENDAR_DIR / ".edit_list.json"

# Look back / ahead window for matching events
_DAYS_BACK = 7
_DAYS_AHEAD = 90
_MAX_CANDIDATES = 8


# ── Search ────────────────────────────────────────────────────────────────────

def _calendars_for_sender(sender_member_id: str) -> list[str]:
    """Calendar IDs to search: family calendar + sender's personal + work (if any)."""
    cals: list[str] = []
    fam = aaka_config.calendar_id()
    if fam:
        cals.append(fam)
    member = None
    for m in aaka_config.members():
        if m.get("id") == sender_member_id:
            member = m
            break
    if member:
        for kind in ("personal", "work", "family"):
            cid = (member.get("calendars", {}).get(kind) or {}).get("id", "")
            if cid and cid not in cals:
                cals.append(cid)
    # Deduplicate while preserving order
    seen, out = set(), []
    for c in cals:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def search_candidates(query: str, sender_member_id: str, *, token_file=None) -> list[dict]:
    """Search calendar events containing `query` in title/location/description.

    Returns a list of candidate dicts:
      {num, event_id, calendar_id, title, start, end, location, attendees}
    """
    from skills.calendar.gog import get_service
    svc = get_service(token_file=token_file)

    now = datetime.datetime.now(datetime.UTC)
    time_min = (now - datetime.timedelta(days=_DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + datetime.timedelta(days=_DAYS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%SZ")

    q = query.strip()
    candidates: list[dict] = []
    seen_ids: set[str] = set()
    for cal_id in _calendars_for_sender(sender_member_id):
        try:
            result = svc.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                q=q if q else None,
                maxResults=25,
            ).execute()
        except Exception:
            continue
        for ev in result.get("items", []):
            eid = ev.get("id")
            if not eid or eid in seen_ids:
                continue
            seen_ids.add(eid)
            start = ev.get("start", {})
            end = ev.get("end", {})
            candidates.append({
                "event_id":   eid,
                "calendar_id": cal_id,
                "title":      ev.get("summary", "(no title)"),
                "start":      start.get("dateTime") or start.get("date") or "",
                "end":        end.get("dateTime") or end.get("date") or "",
                "location":   ev.get("location", ""),
                "attendees":  [a.get("email", "") for a in ev.get("attendees", [])],
                "description": ev.get("description", ""),
            })

    candidates.sort(key=lambda c: c["start"])
    for i, c in enumerate(candidates[:_MAX_CANDIDATES], 1):
        c["num"] = i
    return candidates[:_MAX_CANDIDATES]


def format_candidate_list(candidates: list[dict], query: str) -> str:
    if not candidates:
        return (f"🔍 No events match '{query}' in the next {_DAYS_AHEAD}d "
                f"or past {_DAYS_BACK}d.\nTry a shorter or different keyword.")
    lines = [f"🔍 Events matching '{query}':", ""]
    for c in candidates:
        when = _format_when(c["start"], c["end"])
        loc = f"  📍 {c['location']}" if c.get("location") else ""
        lines.append(f"{c['num']}. {c['title']}\n   {when}{loc}")
    lines.append("")
    lines.append("Reply: /cal edit <N> <changes>")
    lines.append("Changes: + @alex · time 18:00-20:00 · date 2026-06-20 · title: New · loc: New · desc: New · private · no-email")
    return "\n".join(lines)


def _format_when(start: str, end: str) -> str:
    """Render an event start–end span concisely."""
    if not start:
        return "?"
    if "T" in start:
        try:
            s = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None
            head = s.strftime("%a %d %b %H:%M")
            if e:
                if e.date() == s.date():
                    return f"{head}–{e.strftime('%H:%M')}"
                return f"{head} → {e.strftime('%a %d %b %H:%M')}"
            return head
        except ValueError:
            return start
    # All-day
    return start


# ── Persistence ───────────────────────────────────────────────────────────────

def save_edit_list(sender_id: str, candidates: list[dict]) -> None:
    EDIT_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if EDIT_LIST_PATH.exists():
        try:
            existing = json.loads(EDIT_LIST_PATH.read_text())
        except Exception:
            existing = {}
    existing[sender_id] = candidates
    EDIT_LIST_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


def load_edit_item(sender_id: str, num: int) -> dict | None:
    if not EDIT_LIST_PATH.exists():
        return None
    try:
        data = json.loads(EDIT_LIST_PATH.read_text())
    except Exception:
        return None
    for item in data.get(sender_id, []):
        if item.get("num") == num:
            return item
    return None


# ── Modifier parser ───────────────────────────────────────────────────────────

_TIME_RE = re.compile(r'\btime\s+(\d{1,2}:\d{2})(?:\s*-\s*(\d{1,2}:\d{2}))?\b', re.I)
_DATE_RE = re.compile(r'\bdate\s+(\d{4}-\d{2}-\d{2})\b', re.I)
_TITLE_RE = re.compile(r'\btitle:\s*(.+?)(?=\n|$)', re.I)
_LOC_RE = re.compile(r'\bloc(?:ation)?:\s*(.+?)(?=\n|$)', re.I)
_DESC_RE = re.compile(r'\bdesc(?:ription)?:\s*(.+?)(?=\n|$)', re.I | re.S)
_GUEST_ADD_RE = re.compile(r'\+\s*([@\w\.\-+]+(?:\s+[@\w\.\-+]+)*)', re.I)
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def parse_modifiers(text: str, sender_member_id: str) -> dict:
    """Extract edit operations from free-form text.

    Returns a dict with any of:
      add_guests, title, location, description, start_time, end_time,
      date, visibility, send_updates, errors (list of strings)
    """
    out: dict = {"errors": []}
    work = text

    # Date
    m = _DATE_RE.search(work)
    if m:
        out["date"] = m.group(1)
        work = work[:m.start()] + work[m.end():]

    # Time (range or single)
    m = _TIME_RE.search(work)
    if m:
        out["start_time"] = m.group(1)
        if m.group(2):
            out["end_time"] = m.group(2)
        work = work[:m.start()] + work[m.end():]

    # Title / location / description — consume up to end-of-line
    for label, regex, key in (
        ("title", _TITLE_RE, "title"),
        ("loc",   _LOC_RE,   "location"),
        ("desc",  _DESC_RE,  "description"),
    ):
        m = regex.search(work)
        if m:
            out[key] = m.group(1).strip()
            work = work[:m.start()] + work[m.end():]

    # Visibility
    if re.search(r'\bprivate\b', work, re.I):
        out["visibility"] = "private"
    elif re.search(r'\bpublic\b', work, re.I):
        out["visibility"] = "public"

    # No-email
    if re.search(r'\bno-email\b', work, re.I):
        out["send_updates"] = "none"

    # Guest additions — scan all "+ ..." groups
    add_guests: list[str] = []
    for gm in _GUEST_ADD_RE.finditer(work):
        for token in gm.group(1).split():
            tok = token.strip().rstrip(",;")
            if not tok:
                continue
            if _EMAIL_RE.match(tok):
                add_guests.append(tok)
                continue
            if tok.startswith("@"):
                if aaka_config.is_known_guest_tag(tok):
                    resolved = aaka_config.resolve_guests([tok.lower()], sender_member_id)
                    add_guests.extend(resolved)
                else:
                    out["errors"].append(f"Unknown guest tag: {tok}")
    # Deduplicate
    seen = set(); deduped = []
    for g in add_guests:
        if g and g.lower() not in seen:
            seen.add(g.lower()); deduped.append(g)
    if deduped:
        out["add_guests"] = deduped

    return out


# ── Apply ─────────────────────────────────────────────────────────────────────

def apply_changes(candidate: dict, changes: dict, *, token_file=None) -> dict:
    """Call gog.update_event for the candidate using the parsed changes."""
    from skills.calendar.gog import update_event
    return update_event(
        event_id=candidate["event_id"],
        calendar_id=candidate.get("calendar_id", ""),
        title=changes.get("title"),
        description=changes.get("description"),
        location=changes.get("location"),
        add_guests=changes.get("add_guests"),
        visibility=changes.get("visibility"),
        date=changes.get("date"),
        start_time=changes.get("start_time"),
        end_time=changes.get("end_time"),
        send_updates=changes.get("send_updates", "all"),
        token_file=token_file,
    )


def format_change_summary(changes: dict) -> str:
    parts = []
    if changes.get("title"):       parts.append(f"title → {changes['title']}")
    if changes.get("date"):        parts.append(f"date → {changes['date']}")
    if changes.get("start_time"):
        t = changes["start_time"]
        if changes.get("end_time"):
            t += f"–{changes['end_time']}"
        parts.append(f"time → {t}")
    if changes.get("location"):    parts.append(f"loc → {changes['location']}")
    if changes.get("description"): parts.append(f"desc → {changes['description'][:60]}…"
                                                if len(changes['description']) > 60
                                                else f"desc → {changes['description']}")
    if changes.get("add_guests"):  parts.append(f"+guests → {', '.join(changes['add_guests'])}")
    if changes.get("visibility"):  parts.append(f"visibility → {changes['visibility']}")
    if changes.get("send_updates") == "none": parts.append("(silent — no email invites)")
    return " · ".join(parts) if parts else "(no changes detected)"
