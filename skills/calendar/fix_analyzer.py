#!/usr/bin/env python3
"""
Aaka — Fix Analyzer

Scans cached calendar data for issues that need attention:
  1. Conflicts:  overlapping events for the same person
  2. Missing carriers:  events with 🤝 ❓
  3. Unaccepted invites:  attendees who haven't accepted

Public API:
  analyze_fix(period, member_id=None) → list[dict]
  format_fix_list(issues) → str
  save_fix_list(sender_id, issues) → None
  load_fix_item(sender_id, num) → dict | None
"""

import datetime, json, os, re, sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config
from skills.calendar.add_event import analyze_event_title

WEEKLY_CACHE = aaka_config.CALENDAR_DIR / "weekly_events.json"
MEMBER_EVENTS_CACHE = aaka_config.MEMBER_EVENTS_CACHE
FIX_LIST_PATH = aaka_config.CALENDAR_DIR / ".fix_list.json"


def _date_range(period: str) -> tuple[str, str]:
    """Return (start_date, end_date) as YYYY-MM-DD strings for the given period."""
    today = datetime.date.today()
    if period == "today":
        return str(today), str(today)
    # "week": today through end of next Sunday
    days_until_sunday = (6 - today.weekday()) % 7
    if days_until_sunday == 0:
        days_until_sunday = 7  # if today is Sunday, go to next Sunday
    end = today + datetime.timedelta(days=days_until_sunday + 7)
    return str(today), str(end)


def _load_weekly_cache() -> list[dict]:
    if not WEEKLY_CACHE.exists():
        return []
    try:
        return json.loads(WEEKLY_CACHE.read_text())
    except Exception:
        return []


def _load_member_events() -> dict:
    if not MEMBER_EVENTS_CACHE.exists():
        return {}
    try:
        return json.loads(MEMBER_EVENTS_CACHE.read_text())
    except Exception:
        return {}


# ── Conflict detection ────────────────────────────────────────────────────────

def _find_conflicts(start_date: str, end_date: str, member_id: str | None) -> list[dict]:
    """Find intra-member time overlaps: same person, two events at the same time.

    Skips work-to-work overlaps (normal for back-to-back meetings).
    Only flags conflicts where at least one event is family/personal.
    """
    data = _load_member_events()
    issues = []

    members_to_check = [member_id] if member_id else list(data.keys())
    for mid in members_to_check:
        events = data.get(mid, [])
        # Filter to date range and timed events only
        timed = [
            e for e in events
            if start_date <= e.get("date", "") <= end_date
            and e.get("start") and e.get("end")
        ]
        # Sort by date then start time
        timed.sort(key=lambda e: (e["date"], e["start"]))

        # Check each pair on the same date
        for i, a in enumerate(timed):
            for b in timed[i + 1:]:
                if b["date"] != a["date"]:
                    break
                # Skip work-to-work overlaps (normal for calendars with back-to-back meetings)
                if a.get("calendar_type") == "work" and b.get("calendar_type") == "work":
                    continue
                # Overlap: a.start < b.end AND b.start < a.end
                if a["start"] < b["end"] and b["start"] < a["end"]:
                    overlap_start = max(a["start"], b["start"])
                    overlap_end   = min(a["end"],   b["end"])
                    issues.append({
                        "type": "conflict",
                        "member": mid,
                        "date": a["date"],
                        "overlap_start": overlap_start,
                        "overlap_end":   overlap_end,
                        "event_a": {"title": a["title"] or f"({a.get('calendar_type', 'event')})", "start": a["start"], "end": a["end"],
                                    "event_id": a.get("event_id", ""), "calendar_id": a.get("calendar_id", "")},
                        "event_b": {"title": b["title"] or f"({b.get('calendar_type', 'event')})", "start": b["start"], "end": b["end"],
                                    "event_id": b.get("event_id", ""), "calendar_id": b.get("calendar_id", "")},
                    })
    # Deduplicate by title pair
    seen = set()
    deduped = []
    for issue in issues:
        key = (issue["date"],
               min(issue["event_a"]["title"], issue["event_b"]["title"]),
               max(issue["event_a"]["title"], issue["event_b"]["title"]))
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped


# ── Missing carrier detection ────────────────────────────────────────────────

def _carrier_emails() -> set[str]:
    """All known email addresses belonging to carrier members."""
    emails: set[str] = set()
    for m in aaka_config.members():
        if not m.get("is_carrier"):
            continue
        cals = m.get("calendars", {})
        if cals:
            pid = (cals.get("personal") or {}).get("id", "")
            if pid:
                emails.add(pid)
            wid = (cals.get("work") or {}).get("id", "")
            if wid:
                emails.add(wid)
        if m.get("email"):
            emails.add(m["email"])
    return emails


def _find_missing_carriers(start_date: str, end_date: str, member_id: str | None) -> list[dict]:
    """Find events with 🤝 ❓ (carrier needed but not assigned).

    Two sources of carrier_missing:
    - Explicit: title contains '🤝 ❓' — system-set, authoritative.
    - Inferred: attendee present but no 🤝 in title at all — check response_status
      before flagging. If any carrier email is already a guest, the carrier is
      present (may still be unaccepted, but not missing).
    """
    cache = _load_weekly_cache()
    known_carrier_emails = _carrier_emails()
    issues = []
    for ev in cache:
        if not (start_date <= ev.get("date", "") <= end_date):
            continue
        analysis = analyze_event_title(ev.get("title", ""), ev.get("description", ""))
        if not analysis["carrier_missing"]:
            continue
        # If carrier_missing was inferred (no 🤝 marker in title), check whether
        # a carrier is already on the invite via response_status.
        explicit_marker = bool(re.search(r'🤝', ev.get("title", "")))
        if not explicit_marker:
            response_status = ev.get("response_status", {})
            if any(e in known_carrier_emails for e in response_status):
                continue  # carrier already invited — not missing
        # If member_id filter: show if member is an attendee OR a carrier
        if member_id:
            member_name = _member_name(member_id)
            is_carrier = member_name in [c.lower() for c in aaka_config.carriers()]
            is_attendee = member_name and member_name in [a.lower() for a in analysis["attendees"]]
            if not is_attendee and not is_carrier:
                continue
        attendee = analysis["attendees"][0] if analysis["attendees"] else ""
        issues.append({
            "type": "carrier_missing",
            "event_id": ev.get("event_id", ""),
            "calendar_id": ev.get("calendar_id", ""),
            "title": ev.get("title", ""),
            "date": ev["date"],
            "start": ev.get("start", ""),
            "end": ev.get("end", ""),
            "attendee": attendee,
        })
    return issues


# ── Unaccepted invite detection ──────────────────────────────────────────────

def _find_unaccepted(start_date: str, end_date: str, member_id: str | None) -> list[dict]:
    """Find events where invitees haven't accepted."""
    cache = _load_weekly_cache()
    issues = []
    for ev in cache:
        if not (start_date <= ev.get("date", "") <= end_date):
            continue
        rs = ev.get("response_status", {})
        if not rs:
            continue
        pending = []
        att_names = ev.get("attendee_names") or {}
        for email, status in rs.items():
            if status in ("needsAction", "tentative"):
                name = att_names.get(email) or aaka_config.name_by_email(email) or email
                pending.append(name)
        if not pending:
            continue
        # If member_id filter: only show if that member is pending
        if member_id:
            member_name = _member_name(member_id)
            if member_name and member_name not in [p.lower() for p in pending]:
                continue
        issues.append({
            "type": "unaccepted",
            "event_id": ev.get("event_id", ""),
            "calendar_id": ev.get("calendar_id", ""),
            "title": ev.get("title", ""),
            "date": ev["date"],
            "start": ev.get("start", ""),
            "end": ev.get("end", ""),
            "pending": pending,
            "private_member": ev.get("private_member", ""),
        })
    return issues


def _member_name(member_id: str) -> str:
    """Resolve member_id to lowercase display name."""
    for m in aaka_config.members():
        if m.get("id") == member_id:
            return m["name"].lower()
    return member_id.lower()


def _format_date(date_str: str) -> str:
    """Format YYYY-MM-DD as 'Tue 28 Apr'."""
    try:
        d = datetime.date.fromisoformat(date_str)
        return d.strftime("%a %d %b")
    except Exception:
        return date_str


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_fix(period: str = "week", member_id: str | None = None) -> list[dict]:
    """
    Analyze the calendar for issues in the given period.

    Args:
        period: "today" or "week"
        member_id: if set, scope issues to this member only

    Returns numbered list of issues (each dict has "num" key).
    """
    start_date, end_date = _date_range(period)

    conflicts = _find_conflicts(start_date, end_date, member_id)
    carriers = _find_missing_carriers(start_date, end_date, member_id)
    unaccepted = _find_unaccepted(start_date, end_date, member_id)

    # Merge and number
    all_issues = []
    for c in conflicts:
        all_issues.append({
            "num": 0,  # assigned below
            "type": "conflict",
            "event_id": c["event_a"].get("event_id", ""),
            "calendar_id": c["event_a"].get("calendar_id", ""),
            "title": c["event_a"]["title"],
            "date": c["date"],
            "start": c["event_a"]["start"],
            "end": c["event_a"]["end"],
            "member": c["member"],
            "overlap_start": c.get("overlap_start", ""),
            "overlap_end":   c.get("overlap_end", ""),
            "event_a": c["event_a"],
            "event_b": c["event_b"],
            "detail": f"{c['event_a']['title']} {c['event_a']['start']}–{c['event_a']['end']} vs {c['event_b']['title']} {c['event_b']['start']}–{c['event_b']['end']}",
        })
    for m in carriers:
        all_issues.append({
            "num": 0,
            "type": "carrier_missing",
            "event_id": m["event_id"],
            "calendar_id": m["calendar_id"],
            "title": m["title"],
            "date": m["date"],
            "start": m.get("start", ""),
            "end": m.get("end", ""),
            "member": "family",
            "attendee": m["attendee"],
            "detail": f"{m['attendee']} needs a carrier for {m['title']}",
        })
    for u in unaccepted:
        # Attribute to private_member if known, else "family"
        u_member = u.get("private_member") or "family"
        all_issues.append({
            "num": 0,
            "type": "unaccepted",
            "event_id": u["event_id"],
            "calendar_id": u["calendar_id"],
            "title": u["title"],
            "date": u["date"],
            "start": u.get("start", ""),
            "end": u.get("end", ""),
            "member": u_member,
            "pending": u["pending"],
            "detail": f"{', '.join(u['pending'])} hasn't accepted \"{u['title']}\"",
        })

    # Sort: member → date → type (❓ → 👻 → 🧱) → start — matches display order so numbers are sequential
    _TYPE_ORDER = {"carrier_missing": 0, "unaccepted": 1, "conflict": 2}
    all_issues.sort(key=lambda x: (
        x.get("member", ""),
        x["date"],
        _TYPE_ORDER.get(x["type"], 9),
        x.get("start", ""),
    ))

    # Number them 1-based
    for i, issue in enumerate(all_issues, 1):
        issue["num"] = i

    return all_issues


def format_fix_list(issues: list[dict]) -> str:
    """Format issues grouped by member → date, ordered: ❓ → 👻 → 🧱."""
    if not issues:
        return "✅ All clear — no conflicts, missing carriers, or pending invites."

    from collections import defaultdict

    # Legend (only when more than one type present); order matches display order
    types_present = {i["type"] for i in issues}
    legend_parts = []
    if "carrier_missing" in types_present:
        legend_parts.append("❓ no carrier")
    if "unaccepted" in types_present:
        legend_parts.append("👻 not accepted")
    if "conflict" in types_present:
        legend_parts.append("🧱 overlap")

    lines = []
    if len(legend_parts) > 1:
        lines.append("  ".join(legend_parts))
        lines.append("")

    # Group by member — preserve sorted order (issues already sorted by type→member→date)
    by_member: dict = defaultdict(list)
    for issue in issues:
        by_member[issue.get("member") or "family"].append(issue)

    # Preserve member encounter order from the sorted list
    seen_members: list = []
    for issue in issues:
        m = issue.get("member") or "family"
        if m not in seen_members:
            seen_members.append(m)

    for member in seen_members:
        m_obj = aaka_config.member_by_name(member) or {}
        display = m_obj.get("name", member.capitalize())
        lines.append(f"⚡ {display}")

        by_date: dict = defaultdict(list)
        for issue in by_member[member]:
            by_date[issue["date"]].append(issue)

        clash_nums: list[int] = []
        carrier_missing_nums: list[int] = []

        for date in sorted(by_date.keys()):
            lines.append(f" {_format_date(date)}")
            for issue in by_date[date]:
                n = issue["num"]
                if issue["type"] == "carrier_missing":
                    attendee = issue.get("attendee", "")
                    title_short = re.sub(r'🤝\s*❓\s*', '', issue["title"]).strip()
                    time_str = f" {issue['start']}–{issue['end']}" if issue.get("start") and issue.get("end") else ""
                    lines.append(f"{n}. ❓ {attendee} @{title_short}{time_str} — who's taking {attendee}?")
                    carrier_missing_nums.append(n)

                elif issue["type"] == "unaccepted":
                    pending = issue.get("pending", [])
                    time_str = f" {issue['start']}–{issue['end']}" if issue.get("start") and issue.get("end") else ""
                    who = pending[0] if len(pending) == 1 else ", ".join(pending)
                    lines.append(f"{n}. 👻 {issue['title']}{time_str} — {who} hasn't accepted")

                elif issue["type"] == "conflict":
                    ea = issue["event_a"]
                    eb = issue["event_b"]
                    overlap = issue.get("overlap_start", "")
                    overlap_str = f" ~{overlap}" if overlap else ""
                    eb_short = re.sub(r'🤝\s*❓\s*', '', eb["title"]).strip() or "(event)"
                    lines.append(f"{n}. 🧱 {ea['title']} {ea['start']}–{ea['end']} vs ({eb_short}){overlap_str}")
                    clash_nums.append(n)

        # One hint for all carrier-missing items in this person section
        if carrier_missing_nums:
            names = ", ".join(aaka_config.carriers())
            lines.append(f"→ c fix {carrier_missing_nums[-1]} <name>  names: {names}")

        # One hint for all clashes in this person section
        if clash_nums:
            lines.append(f"→ Clash details: c fix {clash_nums[-1]}")

        lines.append("")

    # Remove trailing blank line
    while lines and lines[-1] == "":
        lines.pop()

    # Last-synced elapsed time from cache file mtime
    try:
        secs = int(datetime.datetime.now().timestamp() - WEEKLY_CACHE.stat().st_mtime)
        if secs < 60:
            age = "just now"
        elif secs < 3600:
            age = f"{secs // 60}m ago"
        elif secs < 86400:
            age = f"{secs // 3600}h ago"
        else:
            age = f"{secs // 86400}d ago"
        lines.append(f"\n📡 synced {age}")
    except Exception:
        pass

    return "\n".join(lines)


# ── Inline merge into calendar markdown ──────────────────────────────────────

def _norm_time(t: str) -> str:
    """'13:00' → '1300', '1300' → '1300'."""
    return t.replace(":", "")


def _parse_date_header(line: str) -> str | None:
    """Extract ISO date from '**Monday** 04 May 2026'."""
    m = re.match(r'\*\*\w+\*\*\s+(.+)', line)
    if not m:
        return None
    dm = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', m.group(1))
    if not dm:
        return None
    try:
        d = datetime.datetime.strptime(
            f"{dm.group(1)} {dm.group(2)} {dm.group(3)}", "%d %B %Y"
        ).date()
        return d.isoformat()
    except ValueError:
        return None


def _extract_line_time(line: str) -> str | None:
    """Extract HHMM from an event line like '🗳️**1300** ...'."""
    m = re.search(r'\*\*(\d{4})\*\*', line)
    return m.group(1) if m else None


def _title_matches_line(title: str, line: str) -> bool:
    """Check if an issue's event title is recognisable in a calendar line."""
    if not title:
        return True  # untitled — match by time alone
    # Work events show as "*Alex* @ Work" in the line but "(work)" in the issue
    if title.startswith("(") and title.endswith(")"):
        keyword = title.strip("()")
        return keyword.lower() in line.lower()
    # Use first meaningful word of the title
    words = re.findall(r'[\w\u0080-\uffff]+', title)
    return any(w.lower() in line.lower() for w in words[:3]) if words else True


def merge_fixes_inline(md_text: str, issues: list[dict]) -> str:
    """Merge fix issues as inline annotations into calendar markdown.

    - Conflicts: insert '└ N.🧱→ {event_b}' sub-line under event_a's line
    - Unaccepted (standalone): append 👻 to the event line
    - carrier_missing: footer action hint only (the ❓ is already in the title)

    Returns merged markdown with compact action footer.
    """
    if not issues:
        return md_text

    # Index issues by (date, time_hhmm) for fast lookup
    from collections import defaultdict
    by_date_time: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for issue in issues:
        if issue["type"] == "conflict":
            key = (issue["date"], _norm_time(issue["event_a"]["start"]))
            by_date_time[key].append(issue)
        elif issue["type"] in ("unaccepted", "carrier_missing"):
            key = (issue["date"], _norm_time(issue.get("start", "")))
            by_date_time[key].append(issue)

    # Set of event_ids that are unaccepted (for annotating conflict sub-lines)
    unaccepted_ids = {
        i["event_id"] for i in issues
        if i["type"] == "unaccepted" and i.get("event_id")
    }

    lines = md_text.split("\n")
    result: list[str] = []
    current_date: str | None = None
    clash_nums: list[int] = []
    carrier_nums: list[int] = []
    unaccepted_used: set[int] = set()  # issue nums already shown inline

    for line in lines:
        # Date header?
        dh = _parse_date_header(line)
        if dh is not None:
            current_date = dh
            result.append(line)
            continue

        # Event line with a time?
        lt = _extract_line_time(line) if current_date else None
        if lt is None:
            result.append(line)
            continue

        key = (current_date, lt)
        matched = by_date_time.get(key, [])

        # Standalone unaccepted: append 👻 to the event line
        for issue in matched:
            if issue["type"] == "unaccepted" and "👻" not in line:
                # Check title matches this line (multiple events at same time)
                if _title_matches_line(issue["title"], line):
                    line = line.rstrip() + " 👻"
                    unaccepted_used.add(issue["num"])
                    break

        result.append(line)

        # Conflict sub-lines
        for issue in matched:
            if issue["type"] != "conflict":
                continue
            ea = issue["event_a"]
            if not _title_matches_line(ea["title"], line):
                continue
            eb = issue["event_b"]
            eb_short = re.sub(r'🤝\s*❓\s*', '', eb["title"]).strip() or "(event)"
            ghost = " 👻" if eb.get("event_id") in unaccepted_ids else ""
            eb_emoji = ""
            # Try to find event_b's emoji from the calendar lines nearby
            for peek in lines:
                if _norm_time(eb["start"]) in peek and _title_matches_line(eb["title"], peek):
                    em = re.match(r'^([^\w*\s][\ufe0f\u200d]*)\s*', peek)
                    if em:
                        eb_emoji = em.group(1)
                    break
            eb_time = _norm_time(eb["start"])
            result.append(f"└ {issue['num']}.🧱→ {eb_emoji}{eb_time} {eb_short}{ghost}")
            clash_nums.append(issue["num"])

        # Collect carrier_missing nums for footer
        for issue in matched:
            if issue["type"] == "carrier_missing":
                if _title_matches_line(issue["title"], line):
                    carrier_nums.append(issue["num"])

    # Compact footer with range notation instead of per-number listing
    footer: list[str] = []
    all_clash = [i["num"] for i in issues if i["type"] == "conflict"]
    all_carrier = [i["num"] for i in issues if i["type"] == "carrier_missing"]
    if all_clash:
        rng = f"{min(all_clash)}..{max(all_clash)}" if len(all_clash) > 1 else str(all_clash[0])
        footer.append(f"→ 🧱 overlap details: c fix N | N is {rng}")
    if all_carrier:
        rng = f"{min(all_carrier)}..{max(all_carrier)}" if len(all_carrier) > 1 else str(all_carrier[0])
        carrier_names = " ".join(aaka_config.carriers())
        footer.append(f"→ ❓ assign carrier: c fix N <name> | N is {rng} | name: {carrier_names}")

    merged = "\n".join(result)
    if footer:
        merged += "\n\n" + "\n".join(footer)
    return merged


def lookup_weekly_event(event_id: str) -> dict | None:
    """Find a single event entry from weekly_events.json by event_id."""
    for ev in _load_weekly_cache():
        if ev.get("event_id") == event_id:
            return ev
    return None


def save_fix_list(sender_id: str, issues: list[dict]) -> None:
    """Persist the fix list so `c fix N <name>` can reference items by number."""
    existing = {}
    if FIX_LIST_PATH.exists():
        try:
            existing = json.loads(FIX_LIST_PATH.read_text())
        except Exception:
            existing = {}
    existing[sender_id] = issues
    FIX_LIST_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


def load_fix_item(sender_id: str, num: int) -> dict | None:
    """Load a specific fix item by number for a sender."""
    if not FIX_LIST_PATH.exists():
        return None
    try:
        data = json.loads(FIX_LIST_PATH.read_text())
    except Exception:
        return None
    items = data.get(sender_id, [])
    for item in items:
        if item.get("num") == num:
            return item
    return None
