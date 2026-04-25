#!/usr/bin/env python3
"""
Aaka — Away skill: LLM extraction, review formatting, pad helpers.

JSON fast path (Gem output):
  prepare_event('{"attendee":...}')  → skips LLM entirely
  prepare_event('[{...},{...}]')     → batch, returns list

NL path (natural language):
  prepare_event('Child physio Tuesday 4pm') → calls extract_events()

CLI:
  ./prepare_event.py '{"attendee":"Child","type":"health","summary":"Physio",...}'
  ./prepare_event.py "Child physio next Tuesday 4pm"
"""

import copy, datetime, json, os, re, sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))

import aaka_config
from llm import call_llm
from skills.calendar.add_event import build_title, _detect_no_carrier, format_event_line


# ── Zero-token type / date / time helpers ─────────────────────────────────────

_TYPE_KEYWORDS: dict[str, set[str]] = {
    "health":   {"physio", "doctor", "dentist", "therapy", "therapist", "hospital",
                 "checkup", "check-up", "appointment", "optician", "kieferortho",
                 "orthopedic", "vaccination", "blood test"},
    "fitness":  {"gym", "yoga", "swim", "swimming", "taekwondo", "tennis",
                 "football", "soccer", "basketball", "sport", "workout",
                 "training", "run", "running", "cycling"},
    "music":    {"guitar", "piano", "violin", "cello", "flute", "drum", "drums",
                 "recital", "concert", "music lesson"},
    "meal":     {"dinner", "lunch", "breakfast", "restaurant", "café", "cafe",
                 "brunch"},
    "party":    {"birthday", "party", "celebration", "gathering"},
    "playdate": {"playdate", "play date"},
    "trip":     {"flight", "travel", "vacation", "holiday", "trip"},
}

_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3,   "apr": 4, "april": 4,
    "may": 5,               "jun": 6, "june": 6,
    "jul": 7, "july": 7,    "aug": 8, "august": 8,
    "sep": 9, "september": 9,"oct": 10, "october": 10,
    "nov": 11, "november": 11,"dec": 12, "december": 12,
}

_month_pat_str = '|'.join(_MONTH_NAMES.keys())
_DATE_RANGE_RE = re.compile(
    r'\b\d{1,2}\s*[-–]\s*\d{1,2}\s+(?:' + _month_pat_str + r')\b'
    r'|\b(?:' + _month_pat_str + r')\s+\d{1,2}\s*[-–]\s*\d{1,2}\b'
    r'|\b\d{1,2}\s+(?:' + _month_pat_str + r')\s+(?:to|-)\s+\d{1,2}\s+(?:' + _month_pat_str + r')\b',
    re.I
)

_TIME_RANGE_RE = re.compile(
    r'\b\d{1,2}\s*[-–]\s*\d{1,2}\s*h\b'                                               # 18-20h
    r'|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*[-–]\s*\d{1,2}(?::\d{2})?(?:\s*h|\s*(?:am|pm))?\b'  # 6pm-20h
    r'|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\s+until\s+\d{1,2}(?::\d{2})?(?:\s*h|\s*(?:am|pm))?\b',  # 6pm until 20h
    re.I
)

_DAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _detect_type(text: str) -> str:
    text_lower = text.lower()
    for type_str, keywords in _TYPE_KEYWORDS.items():
        for kw in keywords:
            if re.search(rf'\b{re.escape(kw)}\b', text_lower):
                return type_str
    return "other"


def _parse_natural_date(text: str) -> "datetime.date | None":
    today = datetime.date.today()
    tl = text.lower()

    if re.search(r'\btoday\b', tl):
        return today
    if re.search(r'\btomorrow\b', tl):
        return today + datetime.timedelta(days=1)

    # ISO date: 2026-04-25
    m = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # "next Tuesday" / "this Friday" / bare day name
    m = re.search(
        r'\b(?:(next|this)\s+)?(' + '|'.join(_DAY_NAMES.keys()) + r')\b', tl
    )
    if m:
        modifier = m.group(1)  # "next", "this", or None
        target_wd = _DAY_NAMES[m.group(2)]
        diff = (target_wd - today.weekday()) % 7
        if diff == 0 and modifier != "this":
            diff = 7  # same day-name → next week unless "this"
        if modifier == "next":
            diff = diff if diff > 0 else 7
        return today + datetime.timedelta(days=diff)

    # "April 25", "25th April", "Apr 25", "25 Apr"
    month_pat = '|'.join(_MONTH_NAMES.keys())
    for pat in [
        rf'\b({month_pat})\s+(\d{{1,2}})\b',
        rf'\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pat})\b',
    ]:
        m = re.search(pat, tl)
        if m:
            g = m.groups()
            if g[0].isdigit():
                day_val, month_key = int(g[0]), g[1]
            else:
                month_key, day_val = g[0], int(g[1])
            month_val = _MONTH_NAMES.get(month_key, 0)
            if month_val:
                try:
                    candidate = datetime.date(today.year, month_val, day_val)
                    if candidate < today:
                        candidate = datetime.date(today.year + 1, month_val, day_val)
                    return candidate
                except ValueError:
                    pass

    return None


def _parse_natural_time(text: str) -> "tuple[str, str] | None":
    """Return (start_HH:MM, end_HH:MM) or None if no time found (all-day)."""
    # 4pm / 4:30pm / 16:00 / 16:30
    m = re.search(
        r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b',
        text, re.I
    )
    if m:
        h = int(m.group(1))
        mins = int(m.group(2)) if m.group(2) else 0
        meridiem = m.group(3).lower()
        if meridiem == "pm" and h != 12:
            h += 12
        elif meridiem == "am" and h == 12:
            h = 0
        start = datetime.datetime(1900, 1, 1, h, mins)
        end   = start + datetime.timedelta(hours=1)
        return start.strftime("%H:%M"), end.strftime("%H:%M")

    m = re.search(r'\b(\d{2}):(\d{2})\b', text)
    if m:
        h, mins = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mins <= 59:
            start = datetime.datetime(1900, 1, 1, h, mins)
            end   = start + datetime.timedelta(hours=1)
            return start.strftime("%H:%M"), end.strftime("%H:%M")

    return None


_TIME_TOKEN_RE = re.compile(
    r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b'
    r'|\b\d{2}:\d{2}\b'
    r'|\b(?:' + '|'.join(_DAY_NAMES.keys()) + r')\b'
    r'|\b(?:' + '|'.join(_MONTH_NAMES.keys()) + r')\b'
    r'|\b\d{1,2}(?:st|nd|rd|th)?\b'
    r'|\b\d{4}-\d{2}-\d{2}\b'
    r'|\b(?:today|tomorrow|next|this)\b',
    re.I
)

_STRIP_PREFIXES_RE = re.compile(r'^/(cal|add)\s*', re.I)


def _extract_summary(text: str, attendee: str, carrier: str, type_str: str) -> str:
    """Strip noise tokens → title-case remaining words (max 4)."""
    t = _STRIP_PREFIXES_RE.sub('', text).strip()
    t = re.sub(r'#\w+\s*', '', t)

    # remove carrier verbs phrase: "Gran takes Child to ..."
    if carrier:
        t = re.sub(
            rf'\b{re.escape(carrier)}\b\s+{_CARRIER_VERBS}\s+(?:\b{re.escape(attendee)}\b\s+)?(?:to\s+)?',
            '', t, flags=re.I
        )
    # remove attendee name
    if attendee:
        t = re.sub(rf'\b{re.escape(attendee)}\b', '', t, flags=re.I)
    # remove carrier verbs standalone
    t = re.sub(_CARRIER_VERBS, '', t, flags=re.I)
    # remove time/date tokens
    t = _TIME_TOKEN_RE.sub('', t)
    # remove small words
    t = re.sub(r'\b(to|at|on|the|a|an|with|and|for|of|in|by)\b', '', t, flags=re.I)

    # Separate type keywords from other words — type keywords make good summary too
    kws = _TYPE_KEYWORDS.get(type_str, set())
    all_words = [w.strip('.,!?-') for w in t.split() if len(w.strip('.,!?-')) > 1]
    non_kw = [w for w in all_words if w.lower() not in kws]
    kw_words = [w for w in all_words if w.lower() in kws]

    # Prefer non-keyword words; if none, use the matched keyword itself
    words = non_kw if non_kw else kw_words
    if words:
        return ' '.join(w.title() for w in words[:4])
    return type_str.title()


_GUEST_TAG_RE = re.compile(r'@(\w+)', re.I)
_CONFLICT_MEMBER_RE = re.compile(r'!([A-Z][a-z]+)', re.I)


def _extract_modifiers(text: str) -> "tuple[str, list[str], list[str], list[str], bool]":
    """Extract calendar_tag, guest_tags, unknown_guest_tags, conflict_members, alone.

    Returns (calendar_tag, guest_tags, unknown_guest_tags, conflict_members, is_alone).
    calendar_tag: 'private' → stored as 'private' (mapped to personal at write time).
    guest_tags  : recognised @fam/@all/@work + @<member> tags (lowercased, with @).
    unknown_guest_tags: @-tokens that didn't match any known tag — surfaced as a
                        warning in the confirm message so silent drops never happen.
    """
    # Calendar tag: #private, #personal, #family, etc. — take first
    cal_tag_m = re.search(r'#(\w+)', text)
    calendar_tag = cal_tag_m.group(1).lower() if cal_tag_m else ""
    # #private is user-facing alias; both accepted
    if calendar_tag == "personal":
        calendar_tag = "private"
    # Silently drop #work tag (can't write there)
    if calendar_tag == "work":
        calendar_tag = ""

    guest_tags: list[str] = []
    unknown_guest_tags: list[str] = []
    seen_tags: set[str] = set()
    for m in _GUEST_TAG_RE.finditer(text):
        tag = "@" + m.group(1).lower()
        if tag in seen_tags:
            continue
        seen_tags.add(tag)
        if aaka_config.is_known_guest_tag(tag):
            guest_tags.append(tag)
        else:
            unknown_guest_tags.append(tag)

    conflict_members_raw = [m.group(1) for m in _CONFLICT_MEMBER_RE.finditer(text)]
    conflict_members = []
    for name in conflict_members_raw:
        member = aaka_config.member_by_name(name)
        if member:
            conflict_members.append(member["id"])

    is_alone = bool(_detect_no_carrier(text))

    return calendar_tag, guest_tags, unknown_guest_tags, conflict_members, is_alone


def _extract_events_fast(text: str, sender_email: str = "", sender_member_id: str = "") -> "dict | None":
    """
    Zero-token event extraction via regex + keyword maps.
    Returns None if date can't be determined (triggers LLM fallback).
    """
    calendar_tag, guest_tags, unknown_guest_tags, conflict_members, is_alone = _extract_modifiers(text)

    # Strip all modifier tokens from working copy
    t = re.sub(r'#\w+\s*', '', text).strip()
    t = re.sub(r'@\w+\s*', '', t).strip()
    t = re.sub(r'!\w+\s*', '', t).strip()
    t = _STRIP_PREFIXES_RE.sub('', t).strip()

    carrier  = _detect_carrier(t)
    attendee = _detect_attendee(t, skip=carrier or "") or aaka_config.group_name()

    # Ranges must go to LLM — fast path can't distinguish start from end
    if _DATE_RANGE_RE.search(t):
        return None
    if _TIME_RANGE_RE.search(t):
        return None

    date = _parse_natural_date(t)
    if date is None:
        return None  # LLM fallback

    time_result = _parse_natural_time(t)
    start_time = time_result[0] if time_result else ""
    end_time   = time_result[1] if time_result else ""

    type_str = _detect_type(t)
    summary  = _extract_summary(t, attendee, carrier or "", type_str)

    carrier_required = (attendee in aaka_config.needs_carrier()) and not is_alone
    title = build_title(attendee, type_str, summary,
                        sender_email=sender_email, carrier_override=carrier or "",
                        no_carrier=is_alone)

    # Build guest list: base attendee/carrier guests + @tag guests
    base_guests = aaka_config.guest_emails_for(attendee, carrier or "", start_time=start_time, date=date.isoformat() if date else "")
    tag_guests  = aaka_config.resolve_guests(guest_tags, sender_member_id, start_time=start_time) if guest_tags else []
    seen: set[str] = set()
    guests = []
    for g in base_guests + tag_guests:
        if g not in seen:
            seen.add(g)
            guests.append(g)

    description = "(going alone)" if is_alone else ""
    attendee_id = next((m["id"] for m in aaka_config.members() if m["name"] == attendee), "")

    return {
        "title":            title,
        "attendee":         attendee,
        "attendee_id":      attendee_id,
        "carrier":          carrier or "",
        "carrier_required": carrier_required,
        "type":             type_str,
        "summary":          summary,
        "occurrences":      [{"date": date.isoformat(), "start_time": start_time, "end_time": end_time}],
        "location":         "",
        "description":      description,
        "timezone":         aaka_config.timezone(),
        "guests":           guests,
        "guest_tags":       guest_tags,
        "unknown_guest_tags": unknown_guest_tags,
        "calendar_tag":     calendar_tag,
        "conflict_members": conflict_members,
        "_source":          "zero_token",
    }


# ── Zero-token carrier / attendee detection ───────────────────────────────────

_CARRIER_VERBS = r"(takes?|drives?|brings?|drops?|dropping|picks?\s*up|picking|pickups?|bringing|taking)"


def _detect_carrier(text: str) -> str | None:
    """Detect carrier from 'carrier: Alice', 'Alice takes', 'Bob drives', 'with Alice'."""
    # Explicit syntax: "carrier: <name>", "carrier:<name>", or "carrier <name>"
    m = re.search(r'\bcarrier\s*:?\s+(\w+)', text, re.I)
    if m:
        candidate = m.group(1).strip()
        for name in aaka_config.member_names():
            if name.lower() == candidate.lower():
                return name
    # Verb-based: "Alice takes", "Bob drives", etc.
    for name in aaka_config.member_names():
        if re.search(rf'\b{re.escape(name)}\b\s+{_CARRIER_VERBS}', text, re.I):
            return name
        if re.search(rf'{_CARRIER_VERBS}\s+\b{re.escape(name)}\b', text, re.I):
            return name
    # "with <name>" — companion/carrier (known carriers only, longest name first)
    for name in sorted(aaka_config.carriers(), key=len, reverse=True):
        if re.search(rf'\bwith\s+{re.escape(name)}\b', text, re.I):
            return name
    return None


def _detect_attendee(text: str, skip: str = "") -> str | None:
    """Zero-token scan — return earliest-in-text member; skip the carrier name."""
    text_lower = text.lower()
    best_name: "str | None" = None
    best_pos = len(text_lower) + 1
    for name in aaka_config.member_names():
        if skip and name.lower() == skip.lower():
            continue
        m = re.search(rf'\b{re.escape(name.lower())}\b', text_lower)
        if m:
            pos = m.start()
            if pos < best_pos or (pos == best_pos and len(name) > len(best_name or "")):
                best_pos = pos
                best_name = name
    return best_name


# ── LLM extraction ────────────────────────────────────────────────────────────

_EVENT_TYPE_HELP = """\
  meal    → restaurant, dinner, lunch, breakfast, café
  playdate→ child visiting someone
  visit   → journey, outing, visiting friends
  party   → birthday, celebration, gathering, social event
  music   → concert, recital, lesson, practice (guitar, piano, etc.)
  health  → doctor, dentist, physio, therapy, hospital, check-up, appointment
  fitness → gym, sport, taekwondo, yoga, swimming, workout
  trip    → travel, flight, vacation
  other   → anything else"""

_EVENT_FIELDS_HELP = """\
  attendee     — one of the configured member names (see aaka.yaml), or the shared group name
                 (pick most specific person; the group name = whole household)
  type         — one of the event types above
  summary      — 2–4 word label, e.g. "Physio", "Guitar Class", "Dinner with Meinharts"
                 do NOT repeat the attendee's name in the summary
  occurrences  — array of objects, one per date:
                   date       — "YYYY-MM-DD" (start date)
                   end_date   — "YYYY-MM-DD" last day of range (only for multi-day spans like "18-22 May"); omit for single-day events
                   start_time — "HH:MM" 24h, or "" for all-day
                   end_time   — "HH:MM" 24h (default: start_time + 1h), or ""
For date ranges (e.g. "18-22 May", "Oct 31 to Dec 12"), emit a SINGLE occurrence with date=first day, end_date=last day, and leave start_time/end_time="" (all-day) unless a time is explicitly mentioned.
  location     — venue / address, or ""
  description  — one-sentence description, or ""
  timezone     — the configured timezone (see aaka.yaml) unless city implies otherwise"""


def _build_event_payload(data: dict, sender_email: str, sender_member_id: str,
                         calendar_tag: str, guest_tags: list, conflict_members: list,
                         is_alone: bool, detected_carrier: str,
                         unknown_guest_tags: "list[str] | None" = None) -> dict:
    """Convert a raw LLM event dict into a full payload dict."""
    attendee = data.get("attendee") or aaka_config.group_name()
    type_str = data.get("type", "other")
    summary  = data.get("summary", "Event")
    carrier  = detected_carrier or ""
    carrier_required = (attendee in aaka_config.needs_carrier()) and not is_alone
    title    = build_title(attendee, type_str, summary,
                           sender_email=sender_email, carrier_override=carrier,
                           no_carrier=is_alone)
    first_occ   = (data.get("occurrences") or [{}])[0]
    first_start = first_occ.get("start_time", "")
    first_date  = first_occ.get("date", "")
    base_guests = aaka_config.guest_emails_for(attendee, carrier, start_time=first_start, date=first_date)
    tag_guests  = aaka_config.resolve_guests(guest_tags, sender_member_id, start_time=first_start) if guest_tags else []
    seen_g: set[str] = set()
    guests = []
    for g in base_guests + tag_guests:
        if g not in seen_g:
            seen_g.add(g)
            guests.append(g)
    attendee_id = next((m["id"] for m in aaka_config.members() if m["name"] == attendee), "")
    base_desc = data.get("description", "") or ""
    description = (base_desc + "\n(going alone)").strip() if is_alone else base_desc
    return {
        "title":            title,
        "attendee":         attendee,
        "attendee_id":      attendee_id,
        "carrier":          carrier,
        "carrier_required": carrier_required,
        "type":             type_str,
        "summary":          summary,
        "occurrences":      data.get("occurrences", []),
        "location":         data.get("location", ""),
        "description":      description,
        "timezone":         data.get("timezone", aaka_config.timezone()),
        "guests":           guests,
        "guest_tags":       guest_tags,
        "unknown_guest_tags": list(unknown_guest_tags or []),
        "calendar_tag":     calendar_tag,
        "conflict_members": conflict_members,
        "_via_llm":         True,
    }


def extract_batch(text: str, sender_email: str = "", sender_member_id: str = "") -> "list[dict]":
    """
    Extract one or more distinct events from natural language text.
    Always returns a list — single event returns a 1-element list.
    Uses LLM; always makes exactly one call regardless of event count.
    Handles consecutive same-day slot merging and natural-language pad instructions.

    JSON fast path: if text (after stripping the /cal command prefix) starts with
    '{' or '[', parse directly without calling the LLM.
    """
    # Strip /cal or /add command prefix before JSON check
    stripped = re.sub(r'^/\w+\s*', '', text.strip(), count=1)
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            events = parsed if isinstance(parsed, list) else [parsed]
            # Convert flat date/start_time/end_time to occurrences if needed
            for ev in events:
                if "occurrences" not in ev and "date" in ev:
                    ev["occurrences"] = [{
                        "date": ev.pop("date"),
                        "start_time": ev.pop("start_time", ""),
                        "end_time": ev.pop("end_time", ""),
                    }]
            return [_normalize_event(ev, sender_member_id) for ev in events]
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to LLM

    today = datetime.date.today().isoformat()
    calendar_tag, guest_tags, unknown_guest_tags, conflict_members, is_alone = _extract_modifiers(text)
    text_for_llm = re.sub(r'#\w+\s*', '', text).strip()
    text_for_llm = re.sub(r'@\w+\s*', '', text_for_llm).strip()
    text_for_llm = re.sub(r'!\w+\s*', '', text_for_llm).strip()

    detected_carrier  = _detect_carrier(text_for_llm)
    detected_attendee = _detect_attendee(text_for_llm, skip=detected_carrier or "")

    hints = []
    if detected_attendee:
        hints.append(f"The attendee is {detected_attendee} — use this exact value.")
    if detected_carrier:
        hints.append(f"The carrier (who is taking them) is {detected_carrier}.")
    attendee_hint = ("\n" + " ".join(hints)) if hints else ""

    _members_str = ", ".join(aaka_config.member_names())
    _tz = aaka_config.timezone()
    prompt = f"""You are a calendar event extractor for a personal assistant. Today is {today}.
The household members are: {_members_str}.
Default timezone: {_tz}. Assume {_tz} unless another city is mentioned.
{attendee_hint}
Convention: "X @ Y" means X is the attendee and Y is the location (not a social-media mention). "with Z" means Z is the carrier/companion taking the attendee.
Extract ALL calendar events mentioned in the text. Rules:
- Each distinct event (different activity, person, or date) is a separate object.
- Same recurring event across multiple dates → one object with multiple occurrences.
- MERGE consecutive same-day appointment slots into one occurrence: e.g. 15:00–15:30 + 15:30–16:00 → one occurrence 15:00–16:00. Only merge slots for the same activity on the same date.
- If the text mentions a time buffer or padding (e.g. "pad35", "45min pad each side", "need 30min either side"), set BOTH pad_before AND pad_after to the same value in MINUTES at the TOP LEVEL of the result object (not inside occurrences). Only set them differently if explicitly asymmetric (e.g. "pad 20 before 40 after"). The occurrence times should be the RAW appointment times — padding is applied separately.

Event types — pick one:
{_EVENT_TYPE_HELP}

Each event object has exactly these fields:
{_EVENT_FIELDS_HELP}
  pad_before   — integer minutes of buffer before appointment start (0 if not mentioned)
  pad_after    — integer minutes of buffer after appointment end (0 if not mentioned)

Return a JSON ARRAY of event objects (even if there is only one event).
Return ONLY the raw JSON array. No markdown fences, no explanation.

Text: {text_for_llm}"""

    raw = call_llm(prompt).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = [parsed]

    payloads = []
    for data in parsed:
        ev = _build_event_payload(
            data, sender_email, sender_member_id,
            calendar_tag, guest_tags, conflict_members,
            is_alone, detected_carrier,
            unknown_guest_tags=unknown_guest_tags,
        )
        pad_before = int(data.get("pad_before") or 0)
        pad_after  = int(data.get("pad_after") or 0)
        # "pad35" means both sides — if only one is set, mirror it
        if pad_before and not pad_after:
            pad_after = pad_before
        elif pad_after and not pad_before:
            pad_before = pad_after
        if pad_before or pad_after:
            ev = apply_event_padding(ev, pad_before, pad_after)
        payloads.append(ev)
    return payloads


def extract_events(text: str, sender_email: str = "", force_llm: bool = False, **kwargs) -> dict:
    """
    Parse natural language into structured event data (single event).
    Tries zero-token fast path first; falls back to LLM for complex input.
    Returns a dict ready to serialise as .pending_event.json.
    Raises ValueError if the LLM returns unparseable output.
    """
    sender_member_id = kwargs.get("sender_member_id", "")
    if not force_llm:
        fast = _extract_events_fast(text, sender_email, sender_member_id=sender_member_id)
        if fast is not None:
            fast["_via_llm"] = False
            return fast

    today = datetime.date.today().isoformat()

    # Extract modifiers before stripping for LLM
    calendar_tag, guest_tags, unknown_guest_tags, conflict_members, is_alone = _extract_modifiers(text)
    text_for_llm = re.sub(r'#\w+\s*', '', text).strip()
    text_for_llm = re.sub(r'@\w+\s*', '', text_for_llm).strip()
    text_for_llm = re.sub(r'!\w+\s*', '', text_for_llm).strip()

    detected_carrier  = _detect_carrier(text_for_llm)
    detected_attendee = _detect_attendee(text_for_llm, skip=detected_carrier or "")

    hints = []
    if detected_attendee:
        hints.append(f"The attendee is {detected_attendee} — use this exact value.")
    if detected_carrier:
        hints.append(f"The carrier (who is taking them) is {detected_carrier}.")
    attendee_hint = ("\n" + " ".join(hints)) if hints else ""

    _members_str = ", ".join(aaka_config.member_names())
    _tz = aaka_config.timezone()
    prompt = f"""You are a calendar event extractor for a personal assistant. Today is {today}.
The household members are: {_members_str}.
Default timezone: {_tz}. Assume {_tz} unless another city is mentioned.
{attendee_hint}
Convention: "X @ Y" means X is the attendee and Y is the location (not a social-media mention). "with Z" means Z is the carrier/companion taking the attendee.
Extract all calendar events from the text. For recurring events list each date separately.

Event types — pick one:
{_EVENT_TYPE_HELP}

Return a JSON object with exactly these fields:
{_EVENT_FIELDS_HELP}

Return ONLY the raw JSON object. No markdown fences, no explanation.

Text: {text_for_llm}"""

    raw = call_llm(prompt).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)

    return _build_event_payload(
        data, sender_email, sender_member_id,
        calendar_tag, guest_tags, conflict_members,
        is_alone, detected_carrier,
        unknown_guest_tags=unknown_guest_tags,
    )


# ── Review formatting ─────────────────────────────────────────────────────────

def _friendly_dt(date: str, start: str, end: str) -> str:
    """Format one occurrence as e.g. 'Tuesday, Mar 10, 16:00 – 17:00'."""
    try:
        d = datetime.date.fromisoformat(date)
        day_str = d.strftime("%A, %b %-d")
    except ValueError:
        day_str = date
    if start:
        return f"{day_str}, {start} – {end}" if end else f"{day_str}, {start}"
    return f"{day_str} (all day)"


def format_batch_review(events: list[dict]) -> str:
    """Format a list of pending events for batch confirmation display with inline conflict hints."""
    n = len(events)
    if n == 0:
        return "❌ No events could be extracted. Try rephrasing or use a simpler format."
    any_conflict = any(ev.get("conflicts") for ev in events)
    lines = [f"{n} event{'s' if n != 1 else ''} to add:"]
    for i, ev in enumerate(events, 1):
        occs = ev.get("occurrences", [])
        if occs:
            occ = occs[0]
            dt = _friendly_dt(occ["date"], occ.get("start_time", ""), occ.get("end_time", ""))
            if len(occs) > 1:
                dt += f" (+{len(occs) - 1} more)"
        else:
            dt = "?"
        status = " ✅" if not ev.get("conflicts") else ""
        lines.append(f"\n{i}. {ev['title']}{status}")
        pad_b = ev.get("pad_before", 0)
        pad_a = ev.get("pad_after", 0)
        if pad_b or pad_a:
            lines.append(f"   🕐 {dt}  (±pad {pad_b}/{pad_a}min)")
        else:
            lines.append(f"   🕐 {dt}")
        if ev.get("location"):
            lines.append(f"   📍 {ev['location']}")
        for c in (ev.get("conflicts") or [])[:2]:
            icon = "🚫" if c.get("is_blocker") else "⚠️"
            lines.append(f"   {icon} clash: {c['title']}  {c['start']}–{c['end']}")

    if any_conflict:
        lines.append("\nReply: yes / yes 2 3 / cancel 1 / cancel")
    else:
        lines.append("\n✅ No conflicts found")
        lines.append("Reply: yes / yes 1 3 / cancel")
    return "\n".join(lines)


def _format_confirm_trailer(ev: dict) -> str:
    occs = ev.get("occurrences", [])
    n = len(occs)
    event_word = "event" if n == 1 else f"{n} events"
    has_time = any(o.get("start_time") for o in occs)
    any_occ_conflict = n > 1 and any(o.get("occ_conflicts") for o in occs)
    opts = []
    if has_time:
        opts.append("pad<N>  e.g. pad30, pad30-60")
    nc = aaka_config.needs_carrier()
    if ev.get("attendee") in nc and not ev.get("carrier"):
        opts.append("carrier <name>")
    opts += ["private", "no-email"]
    if any_occ_conflict:
        lines = [f"\nConfirm adding {event_word}? Reply: yes / skip 2 4 / cancel"]
    else:
        lines = [f"\nConfirm adding {event_word}? Reply: yes / cancel"]
    if opts:
        lines.append("Options: " + "  ·  ".join(opts))
    return "\n".join(lines)


def _short_dt(occ: dict) -> str:
    """Format one occurrence as e.g. 'Fri 25 Apr  14:00–15:00  (1h)'."""
    try:
        d = datetime.date.fromisoformat(occ["date"])
        day_str = d.strftime("%a %-d %b")
    except (ValueError, KeyError):
        day_str = occ.get("date", "?")
    end_date = occ.get("end_date", "")
    if end_date and end_date != occ.get("date", ""):
        try:
            d2 = datetime.date.fromisoformat(end_date)
            end_day_str = d2.strftime("%a %-d %b")
            start = occ.get("start_time", "")
            end   = occ.get("end_time", "")
            if start:
                return f"{day_str} {start} – {end_day_str} {end}".rstrip()
            return f"{day_str} – {end_day_str} (all day)"
        except ValueError:
            pass
    start = occ.get("start_time", "")
    end   = occ.get("end_time", "")
    actual_s = occ.get("_actual_start", "")
    actual_e = occ.get("_actual_end", "")
    if start and end:
        try:
            s = datetime.datetime.strptime(start, "%H:%M")
            e = datetime.datetime.strptime(end, "%H:%M")
            mins = int((e - s).total_seconds() / 60)
            dur = f"{mins // 60}h{mins % 60:02d}m" if mins % 60 else f"{mins // 60}h"
            if actual_s and actual_e and (actual_s != start or actual_e != end):
                return f"{day_str}  {actual_s}  ({start}–{end})"
            return f"{day_str}  {start}–{end}  ({dur})"
        except ValueError:
            return f"{day_str}  {start}–{end}"
    elif start:
        return f"{day_str}  {start}"
    return f"{day_str} (all day)"


def _conflict_label(c: dict) -> str:
    """Format a conflict entry as 'member: title' or just member/title if one is empty."""
    member = c.get("member", "")
    title = c.get("title", "")
    if title and member:
        return f"{member}: {title}"
    return title or member or "conflict"


def format_review(ev: dict) -> str:
    occs  = ev.get("occurrences", [])
    n     = len(occs)
    lines = []

    # Header line: title + LLM indicator
    via_llm = ev.get("_via_llm", False)
    title_line = f"📅 {ev['title']}"
    if via_llm:
        title_line += "  {via LLM}"
    lines.append(title_line)

    # Date/time lines (numbered with inline conflict markers when multiple)
    has_occ_conflicts = n > 1 and any(o.get("occ_conflicts") for o in occs)
    if n == 1:
        lines.append(f"   {_short_dt(occs[0])}")
    elif has_occ_conflicts:
        for i, occ in enumerate(occs, 1):
            occ_conflicts = occ.get("occ_conflicts", [])
            if occ_conflicts:
                blocker = any(c.get("is_blocker") for c in occ_conflicts)
                icon = "🚫" if blocker else "⚠️"
                clash_labels = ", ".join(_conflict_label(c) for c in occ_conflicts[:2])
                lines.append(f"   {i}. {_short_dt(occ)}  {icon} {clash_labels}")
            else:
                lines.append(f"   {i}. {_short_dt(occ)}  ✅")
    else:
        for i, occ in enumerate(occs, 1):
            lines.append(f"   {i}. {_short_dt(occ)}")
    if not via_llm:
        lines.append("   💡 reply with 'llm' to re-process via AI")
    lines.append("")

    # Carrier section (before calendar/guests)
    if ev.get("attendee") in aaka_config.needs_carrier():
        no_carrier = not ev.get("carrier_required", True) or ev.get("no_carrier", False)
        if no_carrier:
            lines.append("🚗 Going alone")
        elif ev.get("carrier"):
            lines.append(f"🚗 Carrier: {ev['carrier']}")
        else:
            carrier_names = " · ".join(aaka_config.carriers()) + " · alone"
            lines.append(f"🚗 Carrier: ❓  (who's taking them? {carrier_names})")

    # Calendar tag
    cal_tag = ev.get("calendar_tag", "") or ""
    if cal_tag in ("private", "personal"):
        cal_display = "#private"
    elif cal_tag:
        cal_display = f"#{cal_tag}"
    else:
        cal_display = "#family"
    lines.append(f"📆 Calendar: {cal_display}")
    if ev.get("private"):
        lines.append("   🔒 Event will be private")
    if ev.get("no_email"):
        lines.append("   🔕 No email invites")

    # Guests
    guests = ev.get("guests", [])
    _member_hint = " · ".join(f"@{n.lower()}" for n in aaka_config.member_names()[:3])
    _guest_hint = f"@fam · @all · @work · {_member_hint}" if _member_hint else "@fam · @all · @work"
    if guests:
        lines.append(f"👥 Guests: {', '.join(guests)}")
        lines.append(f"   (add {_guest_hint} to include more)")
    else:
        lines.append(f"👥 Guests: (none — add {_guest_hint})")

    unknown_tags = ev.get("unknown_guest_tags", [])
    if unknown_tags:
        lines.append(
            f"   ⚠️ Ignored unknown tag(s): {', '.join(unknown_tags)}  "
            f"(known: {_guest_hint})"
        )

    lines.append("")

    # Conflict section
    conflicts          = ev.get("conflicts", [])
    checked_labels     = ev.get("checked_calendar_labels", [])
    conflict_members   = ev.get("conflict_members", [])

    if checked_labels:
        bold_labels = " ".join(f"*{lbl}*" for lbl in checked_labels)
        lines.append(f"⚠️ Conflicts checked: {bold_labels}")
        unchecked = [m for m in aaka_config.member_names()
                     if m.lower() not in {lbl.split("/")[0].lower() for lbl in checked_labels}
                     and m.lower() not in {mid.lower() for mid in conflict_members}]
        if unchecked:
            suggestions = " · ".join(f"!{n}" for n in unchecked[:3])
            lines.append(f"   (add {suggestions} to also check their calendars)")
    elif not conflicts:
        lines.append("   ✅ No conflicts checked (cache not available)")

    if conflicts:
        if has_occ_conflicts:
            n_clashing = sum(1 for o in occs if o.get("occ_conflicts"))
            lines.append(f"   {n_clashing} of {n} dates have conflicts (shown above)")
        else:
            attendee = ev.get("attendee", "")
            carrier  = ev.get("carrier", "")
            blocker_names = {attendee.lower(), carrier.lower()} - {""}
            for c in conflicts:
                member_lower = c.get("member", "").lower()
                is_blocker = c.get("is_blocker", member_lower in blocker_names)
                icon = "🚫" if is_blocker else "⚠️ "
                time_str = "all day" if (c.get("start") == "00:00" and c.get("end") == "24:00") else f"{c['start']}–{c['end']}"
                lines.append(f"{icon} {c.get('member','')} — {c['title']}  {time_str}  "
                             f"{'[overlaps]' if is_blocker else '[nearby]'}")
    else:
        if checked_labels:
            lines.append("   ✅ No conflicts in window")

    # Day events
    day_events = ev.get("day_events", [])
    if day_events:
        lines.append("")
        lines.append("📅 Already on the calendar that day:")
        for de in day_events:
            fmt = format_event_line(de["title"])
            if de["all_day"]:
                lines.append(f"• {fmt}  (all day)")
            else:
                lines.append(f"• {fmt}  {de['start']}–{de['end']}")

    lines.append("")
    lines.append(_format_confirm_trailer(ev))
    return "\n".join(lines)


# ── Pad helpers ────────────────────────────────────────────────────────────────

def parse_pad_modifier(text: str) -> "tuple[int, int] | None":
    m = re.search(r'\bpad(\d+)(?:-(\d+))?\b', text, re.I)
    if not m:
        return None
    before = int(m.group(1))
    after = int(m.group(2)) if m.group(2) else before
    return (before, after)


def apply_event_padding(payload: dict, before_min: int, after_min: int) -> dict:
    """Return a deep-copy of payload with padded times and title/description annotation.

    Actual appointment times are stored per-occurrence in ``_actual_start`` /
    ``_actual_end``.  For single-occurrence events the title is annotated with
    the actual start time; for multi-occurrence events it is left clean (each
    Google Calendar event gets its own description note via ``write_event``).
    """
    payload = copy.deepcopy(payload)
    occs = payload.get("occurrences", [])
    actual_times: list[tuple[str, str]] = []
    for occ in occs:
        if not occ.get("start_time"):
            continue
        try:
            start = datetime.datetime.strptime(occ["start_time"], "%H:%M")
            end_str = occ.get("end_time") or ""
            end = datetime.datetime.strptime(end_str, "%H:%M") if end_str else start + datetime.timedelta(hours=1)

            actual_start = start.strftime("%H:%M")
            actual_end   = end.strftime("%H:%M")
            occ["_actual_start"] = actual_start
            occ["_actual_end"]   = actual_end
            actual_times.append((actual_start, actual_end))

            new_start = start - datetime.timedelta(minutes=before_min)
            new_end   = end   + datetime.timedelta(minutes=after_min)
            # Clamp to [00:00, 23:59]
            midnight = datetime.datetime(1900, 1, 1, 0, 0)
            end_day  = datetime.datetime(1900, 1, 1, 23, 59)
            new_start = max(new_start, midnight)
            new_end   = min(new_end, end_day)

            occ["start_time"] = new_start.strftime("%H:%M")
            occ["end_time"]   = new_end.strftime("%H:%M")

        except ValueError:
            continue

    # Single occurrence: annotate shared title + description
    if len(actual_times) == 1:
        actual_start, actual_end = actual_times[0]
        desc = payload.get("description", "") or ""
        # Strip any previously accumulated "Actual appointment:" lines before adding new one
        desc_lines = [l for l in desc.split("\n") if not l.startswith("Actual appointment:")]
        desc = "\n".join(desc_lines).strip()
        note = f"Actual appointment: {actual_start}–{actual_end}"
        payload["description"] = f"{desc}\n{note}".strip() if desc else note
        title = payload.get("title", "")
        # Strip any previously accumulated (HH:MM) time annotations before adding new one
        title = re.sub(r'\s*\(\d{2}:\d{2}\)', '', title).strip()
        payload["title"] = f"{title} ({actual_start})"
    # Multi occurrence: each occ carries its own _actual_start/_actual_end;
    # description is built per-event at write time in write_event().

    payload["pad_before"] = before_min
    payload["pad_after"]  = after_min
    return payload


# ── Normalize + prepare ───────────────────────────────────────────────────────

def _normalize_event(ev: dict, sender_member_id: str) -> dict:
    """Validate required fields, build title if missing, fill defaults."""
    if "occurrences" not in ev:
        raise ValueError("Missing required field: occurrences")
    if "title" not in ev:
        ev["title"] = build_title(
            ev.get("attendee") or aaka_config.group_name(),
            ev.get("type", "other"),
            ev.get("summary", "Event"),
            carrier_override=ev.get("carrier", ""),
        )
    ev.setdefault("attendee", aaka_config.group_name())
    ev.setdefault("carrier", "")
    ev.setdefault("type", "other")
    ev.setdefault("summary", "Event")
    ev.setdefault("location", "")
    ev.setdefault("description", "")
    ev.setdefault("calendar_tag", "")
    ev.setdefault("timezone", aaka_config.timezone())
    if "guests" not in ev:
        first_occ   = (ev["occurrences"] or [{}])[0]
        first_start = first_occ.get("start_time", "")
        first_date  = first_occ.get("date", "")
        ev["guests"] = aaka_config.guest_emails_for(
            ev["attendee"], ev["carrier"], start_time=first_start, date=first_date
        )
    ev["initiated_by"] = sender_member_id
    return ev


def prepare_event(text: str, sender_member_id: str = "") -> "dict | list[dict]":
    """NL or JSON -> pending event dict(s). JSON bypasses LLM entirely.

    Args:
        text: raw input (JSON object/array from Gem, or natural language)
        sender_member_id: member id of the sender (as configured in aaka.yaml, e.g. "alice")

    Returns:
        Single dict for one event, or list of dicts for batch.
    """
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            events = parsed if isinstance(parsed, list) else [parsed]
            result = [_normalize_event(ev, sender_member_id) for ev in events]
            return result if isinstance(parsed, list) else result[0]
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to NL path

    # NL path — LLM extraction
    sender_email = os.environ.get("OPENCLAW_SENDER", "")
    ev = extract_events(text, sender_email=sender_email)
    ev["initiated_by"] = sender_member_id
    return ev


def describe() -> str:
    return (
        "/cal — add an event to the family calendar\n\n"
        "Usage: /cal [attendee] [type keyword] [date] [time] [location] [modifiers]\n\n"
        f"Attendees: {', '.join(aaka_config.member_names())} — or the shared group name for everyone\n\n"
        "Type keywords (auto-detected):\n"
        "  health: physio, doctor, dentist, therapy, checkup\n"
        "  fitness: gym, yoga, swim, taekwondo, sport, workout\n"
        "  meal: dinner, lunch, breakfast, restaurant\n"
        "  music: guitar, piano, concert, recital\n"
        "  trip: flight, travel, vacation\n\n"
        "Dates: today, tomorrow, next Tuesday, Apr 25, 2026-04-25\n"
        "Times: 3pm, 15:00, 4:30pm\n\n"
        "Modifiers (in message body):\n"
        "  #private / #personal — write to your personal calendar (default: #family)\n"
        "  @fam · @all · @work  — invite groups (fam = your family group, work = CC your work cal)\n"
        "  @alex · @tsu …       — invite a specific member by id or name\n"
        "  !Alex !Tsu           — check conflicts on their calendars (no invite)\n\n"
        "Confirmation modifiers (after the preview):\n"
        "  pad30         — 30min buffer before+after\n"
        "  pad15-30      — 15min before, 30min after\n"
        "  carrier <name> — override who is taking them\n"
        "  private        — mark event private\n"
        "  no-email       — skip guest email invites\n\n"
        "Examples:\n"
        "  /cal Child physio tomorrow 3pm\n"
        "  /cal Child guitar Friday 4pm\n"
        "  /cal dinner friends Saturday 7pm\n"
        "  /cal Gran takes Child to dentist Tuesday 10am\n"
        "  yes pad30 carrier Partner"
    )


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) if sys.argv[1:] else sys.stdin.read().strip()
    result = prepare_event(text, "user")
    print(json.dumps(result, indent=2, ensure_ascii=False))
