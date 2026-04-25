#!/usr/bin/env python3
"""
Aaka — Google Calendar writer (gog = Google cOG).
Creates events on the family calendar using OAuth credentials.
Requires token_aakash.json with calendar.events scope (run reauth.py aakash to upgrade).
"""

import datetime, os
from pathlib import Path
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

import sys
BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config

def get_service(token_file=None):
    if token_file is not None:
        token_path = Path(token_file)
    else:
        auth_ms = aaka_config.auth_members()
        _vps = aaka_config.TOKENS_DIR / "token_vps.json"
        if auth_ms:
            info = aaka_config.auth_for(auth_ms[0]["id"])
            _candidate = info["token_file"] if info else None
            if _candidate and _candidate.exists():
                token_path = _candidate
            elif _vps.exists():
                token_path = _vps  # VPS fallback: member token absent
            else:
                token_path = aaka_config.TOKENS_DIR / "token_aakash.json"
        else:
            token_path = _vps if _vps.exists() else aaka_config.TOKENS_DIR / "token_aakash.json"
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def create_event(
    title: str,
    start: str = "",
    end: str = "",
    location: str = "",
    description: str = "",
    timezone: str = None,
    guests: list[str] | None = None,
    calendar_id: str = "",
    visibility: str = "",
    dont_notify: bool = False,
    # High-level convenience params (used by write_event via add_event.py)
    date: str = "",
    start_time: str = "",
    end_time: str = "",
    end_date: str = "",
    private: bool = False,
    token_file=None,
) -> dict:
    """
    Create a single calendar event. Returns {"id": ..., "link": ...}.

    start / end:
      Timed event  → "YYYY-MM-DDTHH:MM:SS"  (interpreted in `timezone`)
      All-day      → "YYYY-MM-DD"
    calendar_id: target Google Calendar ID; defaults to shared family calendar.
    """
    if timezone is None:
        timezone = aaka_config.timezone()
    if private:
        visibility = "private"
    if os.environ.get("BUTLER_TEST"):
        dont_notify = True

    # Build start/end from high-level params if raw start/end not provided
    if date and not start:
        if start_time:
            start = f"{date}T{start_time}:00"
            if end_time:
                end = f"{date}T{end_time}:00"
            else:
                # default +1h
                sh, sm = int(start_time[:2]), int(start_time[3:])
                eh = sh + 1
                end = f"{date}T{eh:02d}:{sm:02d}:00"
        else:
            # all-day: end is exclusive next day (or end_date + 1 day)
            start = date
            if end_date:
                end_d = datetime.date.fromisoformat(end_date) + datetime.timedelta(days=1)
                end = str(end_d)
            else:
                end_d = datetime.date.fromisoformat(date) + datetime.timedelta(days=1)
                end = str(end_d)

    svc = get_service(token_file=token_file)
    if "T" in start:
        start_body = {"dateTime": start, "timeZone": timezone}
        end_body   = {"dateTime": end,   "timeZone": timezone}
    else:
        start_body = {"date": start}
        end_body   = {"date": end}

    body = {
        "summary":     title,
        "location":    location,
        "description": description,
        "start":       start_body,
        "end":         end_body,
    }
    if guests:
        body["attendees"] = [{"email": g} for g in guests]
    if visibility:
        body["visibility"] = visibility

    target_cal = calendar_id or aaka_config.calendar_id()
    event = svc.events().insert(
        calendarId=target_cal,
        body=body,
        sendUpdates="none" if dont_notify else "all",
    ).execute()
    return {"id": event["id"], "link": event.get("htmlLink", "")}


def create_events(
    occurrences: list[dict],
    title: str,
    location: str = "",
    description: str = "",
    timezone: str = None,
    guests: list[str] | None = None,
    calendar_id: str = "",
    visibility: str = "",
    dont_notify: bool = False,
    token_file=None,
) -> list[dict]:
    """
    Create multiple events sharing the same title/location/description.
    Each occurrence: {"start": "YYYY-MM-DDTHH:MM:SS", "end": "YYYY-MM-DDTHH:MM:SS"}
    Returns list of {"id": ..., "link": ...} dicts.
    calendar_id: target Google Calendar ID; forwarded to each create_event() call.
    """
    return [
        create_event(title, occ["start"], occ["end"],
                     location=location, description=description,
                     timezone=timezone, guests=guests, calendar_id=calendar_id,
                     visibility=visibility, dont_notify=dont_notify,
                     token_file=token_file)
        for occ in occurrences
    ]


def update_event(
    event_id: str,
    calendar_id: str = "",
    title: str | None = None,
    add_guests: list[str] | None = None,
    description: str | None = None,
    visibility: str | None = None,
    location: str | None = None,
    date: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    timezone: str | None = None,
    send_updates: str = "all",
    token_file=None,
) -> dict:
    """
    Patch an existing calendar event. Only provided fields are updated.
    visibility   : "default", "public", "private", "confidential" (Google API values).
    date         : YYYY-MM-DD — when present alongside start_time/end_time, rebuilds
                   the event start/end. For all-day events pass date with no times.
    send_updates : "all" (notify guests) or "none" (silent).
    Returns {"id": ..., "link": ...}.
    """
    svc = get_service(token_file=token_file)
    target_cal = calendar_id or aaka_config.calendar_id()

    event = svc.events().get(calendarId=target_cal, eventId=event_id).execute()

    if title is not None:
        event["summary"] = title
    if add_guests:
        existing = event.get("attendees", [])
        existing_emails = {a["email"].lower() for a in existing}
        for email in add_guests:
            if email.lower() not in existing_emails:
                existing.append({"email": email})
        event["attendees"] = existing
    if description is not None:
        event["description"] = description
    if visibility is not None:
        event["visibility"] = visibility
    if location is not None:
        event["location"] = location

    # Date / time updates — rebuild start/end blocks while preserving the other half
    if date is not None or start_time is not None or end_time is not None:
        tz = timezone or event.get("start", {}).get("timeZone") or aaka_config.timezone()
        # Determine target date (fallback to existing event's date)
        existing_start = event.get("start", {})
        existing_end = event.get("end", {})
        cur_date = date
        if cur_date is None:
            dt_str = existing_start.get("dateTime") or existing_start.get("date") or ""
            cur_date = dt_str[:10]
        # Determine times (fallback to existing event)
        def _existing_time(block: dict) -> str:
            dt = block.get("dateTime") or ""
            return dt[11:16] if len(dt) >= 16 else ""
        cur_start_t = start_time if start_time is not None else _existing_time(existing_start)
        cur_end_t   = end_time   if end_time   is not None else _existing_time(existing_end)
        if cur_start_t and cur_end_t:
            event["start"] = {"dateTime": f"{cur_date}T{cur_start_t}:00", "timeZone": tz}
            event["end"]   = {"dateTime": f"{cur_date}T{cur_end_t}:00",   "timeZone": tz}
        else:
            # All-day
            event["start"] = {"date": cur_date}
            event["end"]   = {"date": cur_date}

    updated = svc.events().update(
        calendarId=target_cal,
        eventId=event_id,
        body=event,
        sendUpdates=send_updates,
    ).execute()
    return {"id": updated["id"], "link": updated.get("htmlLink", "")}


def delete_event(event_id: str, calendar_id: str = "", token_file=None) -> None:
    """Delete a calendar event, notifying guests."""
    svc = get_service(token_file=token_file)
    svc.events().delete(
        calendarId=calendar_id or aaka_config.calendar_id(),
        eventId=event_id,
        sendUpdates="all",
    ).execute()
