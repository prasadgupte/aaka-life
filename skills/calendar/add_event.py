#!/usr/bin/env python3
"""
Aaka — Home skill: title utilities + calendar write entrypoint.
Applies the standard title construction rules (attendee + carrier + type emoji).
Called by executor/queue_worker.py. Also importable by prepare_event.py (sensor).
"""

import os, re, sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config

# ── Title construction ────────────────────────────────────────────────────────

TYPE_EMOJIS = {
    "meal":    "🍱",
    "party":   "🎉",
    "playdate":   "🧸",
    "music":   "🎸",
    "health":  "🏥",
    "fitness": "🏋️‍♂️",
    "pickup":   "🏗️",
    "closure":   "❌",
    "visit":   "🚗",
    "trip":    "🚄",
    "other":   "📅",
}


def build_title(attendee: str, type_str: str, summary: str,
                sender_email: str = "", carrier_override: str = "",
                no_carrier: bool = False) -> str:
    emoji = TYPE_EMOJIS.get(type_str.lower(), "📅")
    if attendee in aaka_config.needs_carrier() and not no_carrier:
        carrier = carrier_override or ""
        if not carrier:
            name_from_sender = aaka_config.name_by_email(sender_email) or ""
            if name_from_sender in aaka_config.carriers():
                carrier = name_from_sender
        carrier_str = f" (🤝 {carrier})" if carrier else " (🤝 ❓)"
    else:
        carrier_str = ""
    # Strip attendee name from summary to avoid duplication
    summary_clean = re.sub(rf'\b{re.escape(attendee)}\b\s*(?:and\s+)?', '', summary, flags=re.I).strip()
    return f"{emoji} {attendee}{carrier_str} @ {summary_clean}"


# ── No-carrier detection ──────────────────────────────────────────────────────

_NO_CARRIER_RE = re.compile(r'\b(no\s+carrier|alone|going\s+alone|no\s+parent)\b|(?<!\w)#alone\b', re.I)


def _detect_no_carrier(text: str) -> bool:
    return bool(_NO_CARRIER_RE.search(text))


# ── Event analysis ────────────────────────────────────────────────────────────

def analyze_event_title(title: str, description: str = "") -> dict:
    """
    Return {"attendees": [...], "carriers": [...], "carrier_missing": bool}
    for a calendar event title.
    """
    nc = aaka_config.needs_carrier()
    combined = f"{title} {description}"
    attendees = [n for n in nc if re.search(rf'\b{re.escape(n)}\b', combined, re.I)]

    m = re.search(r'🤝\s*([^)]+)', title)
    if m:
        carrier_val = m.group(1).strip()
        carriers_found = [] if "❓" in carrier_val else [carrier_val]
        carrier_missing = "❓" in carrier_val
    else:
        carriers_found = []
        carrier_missing = bool(attendees)
        # _NO_CARRIER_RE covers: no carrier, alone, going alone, no parent, #alone
        # Also catch the written-back forms: "No carrier required", "carrier: none"
        if _NO_CARRIER_RE.search(description) or re.search(
            r'\b(no\s+carrier\s+required|carrier\s*:\s*none)\b', description, re.I
        ):
            carrier_missing = False

    return {"attendees": attendees, "carriers": carriers_found, "carrier_missing": carrier_missing}


_WORK_LABELS = {"work meeting", "work meetings"}


def format_event_line(title: str, emoji: str = "", description: str = "",
                      private_member: str = "") -> str:
    """
    Format a single event for WhatsApp/Telegram display.
    - private_member + blank/work title  →  "<member> @ Work"
    - family member names                →  *bold*
    - carrier-missing attendee           →  _*bold-italic*_  (no "[needs carrier]" text)
    Returns: "<emoji> <formatted_title>"  (no leading bullet, no time).
    """
    display = title.strip()
    if private_member and (not display or display.lower() in _WORK_LABELS):
        display = f"{private_member} @ Work"
    analysis = analyze_event_title(display, description)
    missing_attendees = set(analysis["attendees"]) if analysis["carrier_missing"] else set()
    for name in aaka_config.member_names():
        if not re.search(rf'\b{re.escape(name)}\b', display):
            continue
        if name in missing_attendees:
            display = re.sub(rf'\b{re.escape(name)}\b', f"{name}❓", display)
        else:
            display = re.sub(rf'\b{re.escape(name)}\b', f"*{name}*", display)
    prefix = f"{emoji} " if emoji else ""
    return f"{prefix}{display}"


# ── Calendar write entrypoint (executor) ─────────────────────────────────────

def write_event(payload: dict, token_file=None) -> dict:
    """Write events to Google Calendar. Returns {"event_ids": [...], "count": N}."""
    from skills.calendar.gog import create_event, create_events
    import aaka_config as _cfg

    import datetime as _dt
    occurrences = payload.get("occurrences", [])
    cal_id = _cfg.target_calendar_id(
        payload.get("sender_member_id", ""), payload.get("calendar_tag", "")
    )
    private = payload.get("private", False)
    guests = [] if payload.get("no_email") else payload.get("guests", [])

    # Tracability footer
    _bot = _cfg.bot_name()
    _qid = payload.get("_queue_id", "")
    _qhash = f"#{_qid[:8]}" if _qid else ""
    _ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    _footer = f"via {_bot} on {_ts} {_qhash}".strip()
    _base_desc = payload.get("description", "")
    if payload.get("no_carrier"):
        _base_desc = f"{_base_desc}\nNo carrier required".strip() if _base_desc else "No carrier required"
    _description = f"{_base_desc}\n{_footer}".strip() if _base_desc else _footer

    if len(occurrences) == 1:
        occ = occurrences[0]
        eid = create_event(
            title=payload["title"], date=occ["date"],
            start_time=occ.get("start_time", ""), end_time=occ.get("end_time", ""),
            end_date=occ.get("end_date", ""),
            location=payload.get("location", ""), description=_description,
            timezone=payload.get("timezone", _cfg.timezone()),
            calendar_id=cal_id, guests=guests, private=private,
            token_file=token_file,
        )
        return {"event_ids": [eid], "count": 1}
    else:
        # Per-occurrence description: append actual appointment time when padded
        eids = []
        for occ in occurrences:
            occ_desc = _description
            actual_s = occ.get("_actual_start", "")
            actual_e = occ.get("_actual_end", "")
            if actual_s and actual_e:
                occ_desc = f"{occ_desc}\nActual appointment: {actual_s}–{actual_e}".strip()
            eid = create_event(
                title=payload["title"], date=occ["date"],
                start_time=occ.get("start_time", ""), end_time=occ.get("end_time", ""),
                end_date=occ.get("end_date", ""),
                location=payload.get("location", ""), description=occ_desc,
                timezone=payload.get("timezone", _cfg.timezone()),
                calendar_id=cal_id, guests=guests, private=private,
                token_file=token_file,
            )
            eids.append(eid)
        return {"event_ids": eids, "count": len(eids)}

# Skill loader entrypoint alias (executor/skill_loader.py calls execute())
execute = write_event
