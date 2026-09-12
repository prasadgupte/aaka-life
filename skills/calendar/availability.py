#!/usr/bin/env python3
"""
Aaka — Availability checker using Google Calendar API.

Public functions:
  fetch_busy()          — merge busy intervals from multiple calendars (freebusy API)
  find_slots()          — find free time slots within constraints
  conflicts_for_event() — check pending event occurrences for calendar conflicts

CLI:
  ./availability.py --member alice --days 7 --duration 60 --from 09:00 --to 18:00 --weekdays
"""

import sys, datetime, argparse, json
from pathlib import Path
from zoneinfo import ZoneInfo

import os
BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config

def _token_path() -> Path:
    """Resolve the active token path via aaka_config (works on both Mac and VPS)."""
    auth_ms = aaka_config.auth_members()
    if auth_ms:
        info = aaka_config.auth_for(auth_ms[0]["id"])
        if info:
            return Path(info["token_file"])
    return aaka_config.TOKENS_DIR / "token_aakash.json"


def _get_service():
    from skills.calendar.gog import get_service
    return get_service()


def _to_rfc3339(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt.tzinfo is None else dt.isoformat()


def _truncate(title: str, limit: int = 30) -> str:
    return title[:limit] + "\u2026" if len(title) > limit else title


def _member_name_for_cal_id(cal_id: str) -> str:
    """Return display name of the member who owns this calendar ID."""
    for m in aaka_config.members():
        for cal in m.get("calendars", {}).values():
            if isinstance(cal, dict) and cal.get("id") == cal_id:
                return m["name"]
    return ""


# ── fetch_busy ────────────────────────────────────────────────────────────────

def fetch_busy(
    calendar_ids: list[str],
    time_min: datetime.datetime,
    time_max: datetime.datetime,
) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """
    Merge busy intervals from all given calendars into a single sorted list.
    Uses the Google Calendar Freebusy API.
    """
    svc = _get_service()
    body = {
        "timeMin": _to_rfc3339(time_min),
        "timeMax": _to_rfc3339(time_max),
        "items": [{"id": cid} for cid in calendar_ids],
    }
    result = svc.freebusy().query(body=body).execute()
    intervals: list[tuple[datetime.datetime, datetime.datetime]] = []
    for cal_data in result.get("calendars", {}).values():
        for slot in cal_data.get("busy", []):
            start = datetime.datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
            end   = datetime.datetime.fromisoformat(slot["end"].replace("Z", "+00:00"))
            intervals.append((start, end))
    # Merge overlapping intervals
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# ── find_slots ────────────────────────────────────────────────────────────────

def find_slots(
    calendar_ids: list[str],
    start_date: datetime.date,
    end_date: datetime.date,
    duration_minutes: int = 60,
    time_window: tuple[str, str] = ("08:00", "20:00"),
    allowed_days: set[str] | None = None,
    timezone: str = "",
) -> list[dict]:
    """
    Find free time slots within the given constraints.
    Returns list of {"start": datetime, "end": datetime}.
    """
    tz_str = timezone or aaka_config.timezone()
    tz = ZoneInfo(tz_str)
    window_start_h, window_start_m = (int(x) for x in time_window[0].split(":"))
    window_end_h,   window_end_m   = (int(x) for x in time_window[1].split(":"))
    duration = datetime.timedelta(minutes=duration_minutes)

    # Fetch busy intervals for the whole date range
    range_min = datetime.datetime(start_date.year, start_date.month, start_date.day,
                                  0, 0, 0, tzinfo=tz)
    range_max = datetime.datetime(end_date.year, end_date.month, end_date.day,
                                  23, 59, 59, tzinfo=tz)
    busy = fetch_busy(calendar_ids, range_min, range_max)

    slots = []
    current = start_date
    while current <= end_date:
        day_name = current.strftime("%A").lower()
        if allowed_days and day_name not in allowed_days:
            current += datetime.timedelta(days=1)
            continue

        window_start = datetime.datetime(current.year, current.month, current.day,
                                         window_start_h, window_start_m, tzinfo=tz)
        window_end   = datetime.datetime(current.year, current.month, current.day,
                                         window_end_h, window_end_m, tzinfo=tz)

        candidate = window_start
        while candidate + duration <= window_end:
            cand_end = candidate + duration
            overlaps = any(s < cand_end and e > candidate for s, e in busy)
            if not overlaps:
                slots.append({"start": candidate, "end": cand_end})
                candidate = cand_end
            else:
                # Jump past the blocking interval
                blocking_end = min((e for s, e in busy if s < cand_end and e > candidate),
                                   default=cand_end)
                candidate = blocking_end

        current += datetime.timedelta(days=1)
    return slots


# ── conflicts_for_event ───────────────────────────────────────────────────────

def _build_conflicts(
    raw_events: list[dict],
) -> list[dict]:
    """
    Deduplicate and truncate a list of raw conflict dicts.
    Each input dict: {"member": str, "title": str, "start": str, "end": str}
    Same title (case-insensitive) from multiple calendars → shown once.
    """
    seen: set[str] = set()
    result = []
    for ev in raw_events:
        key = ev["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "member": ev["member"],
            "title":  _truncate(ev["title"]),
            "start":  ev["start"],
            "end":    ev["end"],
        })
    return result


def conflicts_for_event(
    calendar_ids: list[str],
    occurrences: list[dict],
    timezone: str = "",
) -> list[dict]:
    """
    For each timed occurrence, return overlapping events from the given calendars.
    Returns deduplicated list of {"member": str, "title": str, "start": str, "end": str}.
    Titles truncated to 30 chars with ellipsis.
    Silently returns [] if token is absent or API call fails.
    """
    if not _token_path().exists() or not calendar_ids:
        return []

    tz_str = timezone or aaka_config.timezone()

    try:
        svc = _get_service()
    except Exception:
        return []

    raw: list[dict] = []
    for occ in occurrences:
        date     = occ.get("date", "")
        st       = occ.get("start_time", "")
        et       = occ.get("end_time", "")
        if not (date and st and et):
            continue  # skip all-day or incomplete occurrences

        time_min = f"{date}T{st}:00"
        time_max = f"{date}T{et}:00"

        for cal_id in calendar_ids:
            member_name = _member_name_for_cal_id(cal_id)
            try:
                result = svc.events().list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    timeZone=tz_str,
                    singleEvents=True,
                ).execute()
                for item in result.get("items", []):
                    if item.get("status") == "cancelled":
                        continue
                    # Free/busy-only calendars return no summary; the slot is
                    # still taken. See fetch_day_events.
                    title = item.get("summary", "").strip() or "Busy"
                    ev_start = item["start"].get("dateTime", item["start"].get("date", ""))
                    ev_end   = item["end"].get("dateTime", item["end"].get("date", ""))
                    # Format times as HH:MM for display
                    def _fmt_time(s: str) -> str:
                        try:
                            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
                            local = dt.astimezone(ZoneInfo(tz_str))
                            return local.strftime("%H:%M")
                        except Exception:
                            return s[:5]
                    raw.append({
                        "member": member_name,
                        "title":  title,
                        "start":  _fmt_time(ev_start),
                        "end":    _fmt_time(ev_end),
                    })
            except Exception:
                continue

    return _build_conflicts(raw)


# ── conflicts_from_cache ──────────────────────────────────────────────────────

WEEKLY_CACHE = aaka_config.CALENDAR_DIR / "weekly_events.json"
MEMBER_EVENTS_CACHE = aaka_config.MEMBER_EVENTS_CACHE


def conflicts_from_cache(
    occurrences: list[dict],
    new_attendees: list[str],
    new_carriers: list[str],
    calendar_scope: str = "personal",
) -> list[dict] | None:
    """
    Check member_events.json for time conflicts with the given members.

    calendar_scope controls which calendar types are checked:
      'family'   → family events + work events during 08–19
      'personal' → personal events only
      'work'/other → all event types

    Returns deduplicated conflict list (same format as conflicts_for_event),
    or None if the cache is absent / unreadable (caller should fall back to live API).
    Falls back to weekly_events.json if member_events.json is absent.
    """
    # Try new per-member cache first
    if MEMBER_EVENTS_CACHE.exists():
        try:
            data = json.loads(MEMBER_EVENTS_CACHE.read_text())
        except Exception:
            data = None
        if data is not None:
            relevant_members = {a.lower() for a in new_attendees} | {c.lower() for c in new_carriers}

            def _allowed_type(cal_type: str, start_hour: int) -> bool:
                if calendar_scope == "family":
                    return cal_type == "family" or (cal_type == "work" and 8 <= start_hour < 19)
                if calendar_scope == "personal":
                    return cal_type == "personal"
                return True  # "work" or unknown → check all

            raw: list[dict] = []
            for occ in occurrences:
                date = occ.get("date", "")
                st   = occ.get("start_time", "")
                et   = occ.get("end_time", "")
                if not (date and st and et):
                    continue
                for mid in relevant_members:
                    for cev in data.get(mid, []):
                        if cev.get("date") != date:
                            continue
                        c_start = cev.get("start", "")
                        c_end   = cev.get("end", "")
                        all_day = not (c_start and c_end)
                        if all_day:
                            c_start, c_end = "00:00", "24:00"
                        start_hour = int(c_start[:2]) if c_start else 0
                        if not _allowed_type(cev.get("calendar_type", "personal"), start_hour):
                            continue
                        if st < c_end and et > c_start:
                            raw.append({
                                "member": mid,
                                "title":  cev.get("title", ""),
                                "start":  c_start,
                                "end":    c_end,
                            })
            return _build_conflicts(raw)

    # Fallback: legacy weekly_events.json (flat list, no calendar_type filtering)
    if not WEEKLY_CACHE.exists():
        return None
    try:
        cache = json.loads(WEEKLY_CACHE.read_text())
    except Exception:
        return None

    involved = set(new_attendees + new_carriers)
    raw_legacy: list[dict] = []
    for occ in occurrences:
        date = occ.get("date", "")
        st   = occ.get("start_time", "")
        et   = occ.get("end_time", "")
        if not (date and st and et):
            continue
        for cev in cache:
            if cev["date"] != date:
                continue
            c_start = cev.get("start", "")
            c_end   = cev.get("end", "")
            if not (c_start and c_end):
                c_start, c_end = "00:00", "24:00"
            cached_members = set(cev.get("attendees", []) + cev.get("carriers", []))
            shared = cached_members & involved
            if not shared:
                continue
            if st < c_end and et > c_start:
                for member in shared:
                    raw_legacy.append({
                        "member": member,
                        "title":  cev["title"],
                        "start":  c_start,
                        "end":    c_end,
                    })
    return _build_conflicts(raw_legacy)


# ── conflicts_in_window ───────────────────────────────────────────────────────

def conflicts_in_window(
    occurrences: list[dict],
    sender_member_id: str,
    extra_member_ids: list[str] | None = None,
    blocker_members: list[str] | None = None,
    window_minutes: int = 60,
) -> "tuple[list[dict], list[str]]":
    """Check cache for conflicts within a ±window_minutes window around each occurrence.

    Returns (conflicts, checked_calendar_labels).
    Each conflict: {"member", "title", "start", "end", "is_blocker": bool}
    checked_calendar_labels: e.g. ["alice/personal", "alice/work", "shared"]
    """
    extra_ids    = extra_member_ids or []
    blocker_set  = {b.lower() for b in (blocker_members or []) if b}

    # Build set of member ids to check
    member_ids_to_check = [sender_member_id] + extra_ids

    # Determine which calendar labels are being checked
    checked_labels: list[str] = []
    seen_labels: set[str] = set()
    for mid in member_ids_to_check:
        for m in aaka_config.members():
            if m.get("id") == mid:
                for key in m.get("calendars", {}).keys():
                    label = f"{mid}/{key}"
                    if label not in seen_labels:
                        seen_labels.add(label)
                        checked_labels.append(label)
    # Also add "family" if shared calendar exists
    if aaka_config.calendar_id():
        if "family" not in seen_labels:
            checked_labels.append("family")

    # Read cache
    data = None
    if MEMBER_EVENTS_CACHE.exists():
        try:
            data = json.loads(MEMBER_EVENTS_CACHE.read_text())
        except Exception:
            data = None

    if data is None:
        # Fallback: weekly_events.json — return empty with labels
        return [], checked_labels

    raw: list[dict] = []
    window = datetime.timedelta(minutes=window_minutes)

    for occ in occurrences:
        date = occ.get("date", "")
        st   = occ.get("start_time", "")
        et   = occ.get("end_time", "")
        if not (date and st and et):
            occ["occ_conflicts"] = []
            continue
        try:
            occ_start = datetime.datetime.strptime(f"{date}T{st}", "%Y-%m-%dT%H:%M")
            occ_end   = datetime.datetime.strptime(f"{date}T{et}", "%Y-%m-%dT%H:%M")
        except ValueError:
            occ["occ_conflicts"] = []
            continue
        win_start = (occ_start - window).strftime("%H:%M")
        win_end   = (occ_end   + window).strftime("%H:%M")

        occ_raw: list[dict] = []
        for mid in member_ids_to_check:
            for cev in data.get(mid, []):
                if cev.get("date") != date:
                    continue
                c_start = cev.get("start", "")
                c_end   = cev.get("end", "")
                all_day = not (c_start and c_end)
                if all_day:
                    c_start, c_end = "00:00", "24:00"
                # In window: c_start < win_end and c_end > win_start
                # All-day events (00:00–24:00) always satisfy this
                if not (c_start < win_end and c_end > win_start):
                    continue
                # is_blocker: member is attendee/carrier AND event directly overlaps
                # All-day events are treated as nearby (not hard blocker)
                directly_overlaps = st < c_end and et > c_start
                is_blocker = directly_overlaps and mid.lower() in blocker_set and not all_day

                entry = {
                    "member":     mid,
                    "title":      cev.get("title", ""),
                    "start":      c_start,
                    "end":        c_end,
                    "is_blocker": is_blocker,
                }
                raw.append(entry)
                occ_raw.append(entry)

        # Per-occurrence dedup
        seen_occ: set[str] = set()
        occ_deduped: list[dict] = []
        for ev in sorted(occ_raw, key=lambda x: (not x["is_blocker"], x["title"].lower())):
            key = ev["title"].lower()
            if key not in seen_occ:
                seen_occ.add(key)
                occ_deduped.append({
                    "member": ev["member"], "title": _truncate(ev["title"]),
                    "start": ev["start"], "end": ev["end"], "is_blocker": ev["is_blocker"],
                })
        occ["occ_conflicts"] = occ_deduped

    # Deduplicate by title (case-insensitive), preferring is_blocker=True
    seen_t: set[str] = set()
    deduped: list[dict] = []
    for ev in sorted(raw, key=lambda x: (not x["is_blocker"], x["title"].lower())):
        key = ev["title"].lower()
        if key in seen_t:
            continue
        seen_t.add(key)
        deduped.append({
            "member":     ev["member"],
            "title":      _truncate(ev["title"]),
            "start":      ev["start"],
            "end":        ev["end"],
            "is_blocker": ev["is_blocker"],
        })
    return deduped, checked_labels


# ── fetch_day_events ──────────────────────────────────────────────────────────

def fetch_day_events(
    calendar_ids: list[str],
    date_str: str,
    timezone: str = "",
) -> list[dict]:
    """
    Return all events (timed + all-day) for a single day across calendar_ids.
    Each item: {"title": str, "start": str, "end": str, "all_day": bool}
    """
    if not _token_path().exists() or not calendar_ids:
        return []
    tz_str = timezone or aaka_config.timezone()
    tz = ZoneInfo(tz_str)
    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        return []
    day_start = datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    day_end   = day_start + datetime.timedelta(days=1)

    try:
        svc = _get_service()
    except Exception:
        return []

    seen: set[tuple[str, str, str]] = set()
    results: list[dict] = []
    for cal_id in calendar_ids:
        try:
            items = svc.events().list(
                calendarId=cal_id,
                timeMin=_to_rfc3339(day_start),
                timeMax=_to_rfc3339(day_end),
                timeZone=tz_str,
                singleEvents=True,
                orderBy="startTime",
            ).execute().get("items", [])
            for item in items:
                if item.get("status") == "cancelled":
                    continue
                # A calendar shared as free/busy only (a work account, typically)
                # returns each event with no summary. That is still a real
                # commitment with a real start and end, so keep it as "Busy"
                # rather than dropping it and reporting the day as empty.
                title = item.get("summary", "").strip() or "Busy"
                all_day = "date" in item["start"]
                # Dedupe on time as well as title: the same invite on two
                # calendars collapses, but two different "Busy" blocks survive.
                key = (
                    title.lower(),
                    item["start"].get("dateTime") or item["start"].get("date", ""),
                    item["end"].get("dateTime") or item["end"].get("date", ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                if all_day:
                    results.append({"title": title, "start": "", "end": "", "all_day": True})
                else:
                    def _fmt(s: str, _tz: ZoneInfo = tz) -> str:
                        try:
                            return datetime.datetime.fromisoformat(
                                s.replace("Z", "+00:00")).astimezone(_tz).strftime("%H:%M")
                        except Exception:
                            return s[:5]
                    results.append({
                        "title": title,
                        "start": _fmt(item["start"]["dateTime"]),
                        "end":   _fmt(item["end"]["dateTime"]),
                        "all_day": False,
                    })
        except Exception:
            continue
    # orderBy is per calendar; across calendars the list arrives interleaved.
    results.sort(key=lambda e: (not e["all_day"], e["start"]))
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(description="Find free slots for a family member.")
    parser.add_argument("--member",   required=True, help="Member ID (e.g. alice — from aaka.yaml)")
    parser.add_argument("--days",     type=int, default=7, help="How many days ahead (default 7)")
    parser.add_argument("--duration", type=int, default=60, help="Slot duration in minutes (default 60)")
    parser.add_argument("--from",     dest="from_time", default="08:00", help="Window start HH:MM")
    parser.add_argument("--to",       dest="to_time",   default="20:00", help="Window end HH:MM")
    parser.add_argument("--weekdays", action="store_true", help="Weekdays only")
    args = parser.parse_args()

    cal_ids = aaka_config.member_calendar_ids(args.member)
    if not cal_ids:
        print(f"No calendars found for member '{args.member}'.")
        sys.exit(1)

    allowed = {"monday","tuesday","wednesday","thursday","friday"} if args.weekdays else None
    today = datetime.date.today()
    end   = today + datetime.timedelta(days=args.days - 1)

    print(f"Finding {args.duration}-min slots for {args.member} over next {args.days} days...")
    slots = find_slots(
        cal_ids, today, end,
        duration_minutes=args.duration,
        time_window=(args.from_time, args.to_time),
        allowed_days=allowed,
    )
    tz_str = aaka_config.timezone()
    tz = ZoneInfo(tz_str)
    if not slots:
        print("No free slots found.")
    for s in slots:
        local_s = s["start"].astimezone(tz)
        local_e = s["end"].astimezone(tz)
        print(f"  {local_s.strftime('%a %d %b  %H:%M')} – {local_e.strftime('%H:%M')}")


if __name__ == "__main__":
    _cli()
