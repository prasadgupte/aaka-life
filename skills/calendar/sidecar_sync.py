#!/usr/bin/env python3
"""
Aaka — Sidecar Sync Script
Runs every 30 minutes via crontab.
Writes: today.md, weekly.md, snapshots/, audit.log
"""

import os, json, datetime, shutil, traceback, sys
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config
from skills.calendar.add_event import analyze_event_title, format_event_line

# ── Paths ──────────────────────────────────────────────
CALENDAR  = aaka_config.CALENDAR_DIR
SNAPSHOTS = aaka_config.SNAPSHOTS_DIR
LOGS      = aaka_config.LOGS_DIR
AUDIT     = LOGS / "audit.log"

SKIP_CACHE_FILE = CALENDAR / "skip_cache.json"
SKIP_TTL_HOURS  = 24


def log(level: str, msg: str):
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {level:<6} {msg}\n"
    with open(AUDIT, "a") as f:
        f.write(line)
    print(line, end="")


def _load_skip_cache() -> dict:
    try:
        return json.loads(SKIP_CACHE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_skip_cache(cache: dict) -> None:
    SKIP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SKIP_CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _is_skipped(cal_id: str, cache: dict) -> bool:
    until = cache.get(cal_id)
    if not until:
        return False
    try:
        return datetime.datetime.fromisoformat(until.replace("Z", "+00:00")) > datetime.datetime.now(datetime.UTC)
    except ValueError:
        return False


def _mark_skip(cal_id: str, cache: dict, reason: str) -> None:
    until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=SKIP_TTL_HOURS)
    cache[cal_id] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_skip_cache(cache)
    log("INFO", f"calendar {cal_id} unreachable ({reason}) — suppressing for {SKIP_TTL_HOURS}h")

def get_service_for(member_id: str):
    info = aaka_config.auth_for(member_id)
    if not info:
        raise ValueError(f"No auth config for {member_id}")
    token_path = info["token_file"]
    # On VPS only token_vps.json exists; fall back when member token is absent
    if not token_path.exists():
        vps_token = aaka_config.TOKENS_DIR / "token_vps.json"
        if vps_token.exists():
            token_path = vps_token
        else:
            raise FileNotFoundError(f"Token not found: {token_path}")
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
        log("INFO", f"[{member_id}] token refreshed")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def get_service():
    """Backwards-compatible: returns service for first auth member."""
    auth_ms = aaka_config.auth_members()
    if auth_ms:
        return get_service_for(auth_ms[0]["id"])
    token_path = aaka_config.TOKENS_DIR / "token_aakash.json"
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def _ev_sort_key(ev: dict) -> str:
    return ev["start"].get("dateTime", ev["start"].get("date", ""))

def _fetch_into(service, days_ahead: int, seen: dict) -> None:
    """Fetch events and merge into `seen` (keyed by event ID)."""
    now  = datetime.datetime.now(datetime.UTC)
    end  = now + datetime.timedelta(days=days_ahead)
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    emoji_map = aaka_config.calendar_emoji_map()
    skip_cache = _load_skip_cache()
    for cal_id in aaka_config.all_calendar_ids():
        if _is_skipped(cal_id, skip_cache):
            continue
        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50
            ).execute()
            for ev in result.get("items", []):
                if ev["id"] not in seen:
                    ev["_emoji"] = emoji_map.get(cal_id, "")
                    ev["_private_member"] = aaka_config.calendar_private_member(cal_id)
                    ev["_cal_id"] = cal_id
                    seen[ev["id"]] = ev
        except HttpError as e:
            if e.status_code == 403:
                _mark_skip(cal_id, skip_cache, "403 permission denied")
                continue
            if e.status_code == 404:
                # Shared but not yet added to calendarList — auto-add and retry once
                try:
                    service.calendarList().insert(body={"id": cal_id}).execute()
                    log("INFO", f"added {cal_id} to calendarList, retrying")
                    result = service.events().list(
                        calendarId=cal_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=50
                    ).execute()
                    for ev in result.get("items", []):
                        if ev["id"] not in seen:
                            ev["_emoji"] = emoji_map.get(cal_id, "")
                            ev["_private_member"] = aaka_config.calendar_private_member(cal_id)
                            ev["_cal_id"] = cal_id
                            seen[ev["id"]] = ev
                    continue
                except Exception:
                    _mark_skip(cal_id, skip_cache, "404 not shared (insert failed)")
                    continue
            _mark_skip(cal_id, skip_cache, f"http {e.status_code}")
        except Exception as e:
            _mark_skip(cal_id, skip_cache, f"exception {type(e).__name__}")

def fetch_events(service, days_ahead: int) -> list:
    seen: dict = {}
    _fetch_into(service, days_ahead, seen)
    return sorted(seen.values(), key=_ev_sort_key)

def merge_work_meetings(events: list) -> list:
    result, i = [], 0
    while i < len(events):
        ev = events[i]
        if (ev.get("summary") or "").strip() in ("", "(no title)"):
            group = [ev]
            while i + len(group) < len(events):
                nxt = events[i + len(group)]
                if (nxt.get("summary") or "").strip() not in ("", "(no title)"):
                    break
                try:
                    last_end  = datetime.datetime.fromisoformat(
                        group[-1]["end"].get("dateTime","").replace("Z","+00:00"))
                    nxt_start = datetime.datetime.fromisoformat(
                        nxt["start"].get("dateTime","").replace("Z","+00:00"))
                    if (nxt_start - last_end).total_seconds() > 15 * 60:
                        break
                except (ValueError, AttributeError):
                    break
                group.append(nxt)
            merged = {**group[0], "end": group[-1]["end"], "_merged": len(group)}
            result.append(merged)
            i += len(group)
        else:
            result.append(ev)
            i += 1
    return result


def _format_duration(minutes: int) -> str:
    rounded = round(minutes / 30) * 30
    if rounded == 0:
        rounded = 30
    if rounded < 60:
        return f"({rounded}m)"
    elif rounded % 60 == 0:
        return f"({rounded // 60}h)"
    else:
        return f"({rounded // 60}h{rounded % 60}m)"


def event_to_md_line(ev: dict) -> str:
    is_all_day = "dateTime" not in ev["start"]
    summary        = (ev.get("summary") or "").strip()
    private_member = ev.get("_private_member", "")
    emoji          = "🎂" if "birthday" in summary.lower() else ev.get("_emoji", "")
    loc_str        = f" 📍 {ev['location']}" if ev.get("location") else ""

    if not summary or summary == "(no title)":
        if private_member:
            raw_title = ""   # format_event_line converts empty+private → "<member> @ Work"
        else:
            raw_title = "Work meetings" if ev.get("_merged", 1) > 1 else "Work meeting"
    else:
        raw_title = summary

    # Pass emoji="" — we prepend it ourselves to control ❗ placement
    body = format_event_line(raw_title, emoji="", description=ev.get("description", ""), private_member=private_member)
    emoji_pfx = f"{emoji}" if emoji else ""

    if is_all_day:
        # All-day: emoji {space} title — no time, no duration
        sep = " " if emoji_pfx else ""
        return f"{emoji_pfx}{sep}{body}{loc_str}"

    start_raw = ev["start"].get("dateTime")
    end_raw   = ev["end"].get("dateTime", ev["end"].get("date"))
    try:
        dt_s = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        dt_e = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        time_bold = f"**{dt_s.strftime('%H%M')}**"
        dur_minutes = int((dt_e - dt_s).total_seconds() / 60)
        dur_str = f" {_format_duration(dur_minutes)}"

        # ❗ outside work hours: weekday AND (start < 09:00 OR end > 16:30)
        weekday = dt_s.weekday() < 5  # Mon–Fri
        start_mins = dt_s.hour * 60 + dt_s.minute
        end_mins   = dt_e.hour * 60 + dt_e.minute
        outside_work = weekday and (start_mins < 9 * 60 or end_mins > 16 * 60 + 30)
        alert = "❗" if outside_work else ""
    except (ValueError, KeyError, AttributeError):
        time_bold = ""
        dur_str = ""
        alert = ""

    if time_bold:
        # Format: {emoji}{❗?}**{HHMM}** {body}{loc_str} ({duration})
        return f"{emoji_pfx}{alert}{time_bold} {body}{loc_str}{dur_str}"
    return f"{emoji_pfx} {body}{loc_str}" if emoji_pfx else f"{body}{loc_str}"

def write_markdown(events: list, path: Path, title: str):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# {title}", f"_Last synced: {now_str}_"]
    if not events:
        lines.append("\n_No events found._")
    else:
        grouped: dict = {}
        for ev in events:
            start = ev["start"].get("dateTime", ev["start"].get("date"))
            try:
                dt = datetime.datetime.fromisoformat(start.replace("Z","+00:00"))
                key = dt.strftime("%A|%d %B %Y")
            except (ValueError, KeyError):
                key = f"?|{start[:10]}"
            grouped.setdefault(key, []).append(ev)
        first = True
        for key, day_evs in grouped.items():
            day_name, date_part = key.split("|", 1)
            if not first:
                lines.append("")
            lines.append(f"**{day_name}** {date_part}")
            for ev in merge_work_meetings(day_evs):
                lines.append(event_to_md_line(ev))
            first = False
    path.write_text("\n".join(lines))

WEEKLY_CACHE = CALENDAR / "weekly_events.json"

def write_weekly_cache(events: list):
    """
    Write calendar/weekly_events.json: structured event data with involved_members.
    Used by availability.py for local conflict checks (avoids live API call).
    """
    entries = []
    for ev in events:
        start_raw = ev["start"].get("dateTime", ev["start"].get("date", ""))
        end_raw   = ev["end"].get("dateTime",   ev["end"].get("date", ""))
        try:
            dt = datetime.datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            date_str  = dt.strftime("%Y-%m-%d")
            start_str = dt.strftime("%H:%M")
            dt_e = datetime.datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            end_str = dt_e.strftime("%H:%M")
        except Exception:
            # All-day event: start_raw is "YYYY-MM-DD"
            date_str  = start_raw[:10]
            start_str = ""
            end_str   = ""
        raw_title      = (ev.get("summary") or "").strip()
        private_member = ev.get("_private_member", "")

        # Skip untitled events only when they're also non-private
        if not raw_title and not private_member:
            continue
        if raw_title == "(no title)" and not private_member:
            continue

        # Determine cache title
        cache_title = raw_title if raw_title else (f"{private_member} @ Work" if private_member else "")

        analysis = analyze_event_title(raw_title)   # analyze original, not formatted
        # Attendee response status (needsAction/accepted/declined/tentative)
        # and display names where available
        response_status = {}
        attendee_names = {}
        for att in ev.get("attendees", []):
            email = att.get("email", "")
            if email:
                response_status[email] = att.get("responseStatus", "needsAction")
                display = att.get("displayName", "").strip()
                local = email.split("@")[0].lower()
                if display and display.lower() != local:
                    attendee_names[email] = display
        entries.append({
            "date":           date_str,
            "start":          start_str,
            "end":            end_str,
            "title":          cache_title,
            "attendees":      analysis["attendees"],
            "carriers":       analysis["carriers"],
            "description":    (ev.get("description") or "").strip(),
            "private_member": private_member,
            "emoji":          ev.get("_emoji", ""),
            "location":       (ev.get("location") or "").strip(),
            "event_id":       ev.get("id", ""),
            "calendar_id":    ev.get("_cal_id", ""),
            "response_status": response_status,
            "attendee_names": attendee_names,
        })
    WEEKLY_CACHE.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def _ev_date(ev: dict) -> str:
    raw = ev["start"].get("dateTime", ev["start"].get("date", ""))
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return raw[:10]


def _ev_time(ev: dict, key: str) -> str:
    raw = ev[key].get("dateTime", "")
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M")
    except Exception:
        return ""


def _build_cal_to_members() -> dict:
    """Return {cal_id: [member_id, ...]} mapping."""
    family_id = aaka_config.calendar_id()
    all_ids = [m["id"] for m in aaka_config.members()]
    cal_to_members: dict = {}
    for m in aaka_config.members():
        for cal_id in aaka_config.member_calendar_ids(m["id"]):
            cal_to_members.setdefault(cal_id, []).append(m["id"])
    cal_to_members[family_id] = all_ids
    return cal_to_members


def write_member_events_cache(events: list):
    """Write member_events.json: {member_id: [event, ...]} for conflict detection."""
    cal_to_members = _build_cal_to_members()
    index = {m["id"]: [] for m in aaka_config.members()}
    for ev in events:
        cal_id = ev.get("_cal_id", "")
        cal_type = aaka_config.calendar_type(cal_id)
        entry = {
            "date":           _ev_date(ev),
            "start":          _ev_time(ev, "start"),
            "end":            _ev_time(ev, "end"),
            "title":          (ev.get("summary") or "").strip(),
            "calendar_type":  cal_type,
            "private_member": ev.get("_private_member", ""),
            "event_id":       ev.get("id", ""),
            "calendar_id":    ev.get("_cal_id", ""),
        }
        for member_id in cal_to_members.get(cal_id, []):
            if member_id in index:
                index[member_id].append(entry)
    aaka_config.MEMBER_EVENTS_CACHE.write_text(json.dumps(index, ensure_ascii=False, indent=2))


def write_member_md_files(events: list):
    """Write today_<member>.md and weekly_<member>.md for each member."""
    from collections import defaultdict
    cal_to_members = _build_cal_to_members()
    member_events: dict = defaultdict(list)
    for ev in events:
        cal_id = ev.get("_cal_id", "")
        for member_id in cal_to_members.get(cal_id, []):
            member_events[member_id].append(ev)

    today_cutoff = datetime.datetime.now(datetime.UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + datetime.timedelta(days=1)
    today_cutoff_str = today_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    for m in aaka_config.members():
        mid = m["id"]
        all_evs = sorted(member_events.get(mid, []), key=_ev_sort_key)
        today_evs = [e for e in all_evs if _ev_sort_key(e) < today_cutoff_str]
        write_markdown(today_evs,  CALENDAR / f"today_{mid}.md",  f"Today — {mid.capitalize()}")
        write_markdown(all_evs,    CALENDAR / f"weekly_{mid}.md", f"This Week — {mid.capitalize()}")


def snapshot(src: Path):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    dest = SNAPSHOTS / f"{src.stem}_{ts}.md"
    shutil.copy2(src, dest)
    # Keep only last 48 snapshots per file type
    all_snaps = sorted(SNAPSHOTS.glob(f"{src.stem}_*.md"))
    for old in all_snaps[:-48]:
        old.unlink()

def check_calendars():
    """Probe each calendar for read access and print OK/DENIED. Exits without writing files."""
    cal_ids = aaka_config.all_calendar_ids()
    auth_ms = aaka_config.auth_members()
    print(f"Checking {len(cal_ids)} calendar(s) across {len(auth_ms)} token(s)...")
    services = {}
    for m in auth_ms:
        try:
            services[m["id"]] = get_service_for(m["id"])
        except Exception as e:
            print(f"  ! could not load token for {m['id']}: {e}")
    any_denied = False
    for cal_id in sorted(cal_ids):
        label = aaka_config.calendar_label(cal_id)
        last_err = None
        ok = False
        for member_id, svc in services.items():
            try:
                svc.events().list(calendarId=cal_id, maxResults=1).execute()
                print(f"  \u2713  {cal_id}  ({label})  [via {member_id}]")
                ok = True
                break
            except HttpError as e:
                last_err = e
            except Exception as e:
                last_err = e
        if not ok:
            e = last_err
            if isinstance(e, HttpError) and e.status_code == 403:
                print(f"  \u2717  {cal_id}  ({label}) \u2014 PERMISSION DENIED (all tokens)")
            else:
                print(f"  \u2717  {cal_id}  ({label}) \u2014 ERROR: {e}")
            any_denied = True
    sys.exit(1 if any_denied else 0)


def sync_holidays(svc) -> None:
    """Fetch public holidays from configured holiday_calendars and cache to holidays.json.

    Each entry in aaka.yaml's holiday_calendars list has:
      id:    Google Calendar ID
      match:
        description: [kw, ...]   # ALL must appear in event description (case-insensitive)
        title:       [kw, ...]   # ALL must appear in event title (case-insensitive); optional
    """
    configs = aaka_config.holiday_calendars()
    if not configs:
        return
    all_dates: set[str] = set()
    now = datetime.datetime.utcnow().isoformat() + "Z"
    end = (datetime.datetime.utcnow() + datetime.timedelta(days=365)).isoformat() + "Z"
    for cal in configs:
        cal_id = cal.get("id", "")
        if not cal_id:
            continue
        match_cfg = cal.get("match", {})
        desc_kws  = [k.lower() for k in match_cfg.get("description", [])]
        title_kws = [k.lower() for k in match_cfg.get("title", [])]
        try:
            result = svc.events().list(
                calendarId=cal_id,
                timeMin=now, timeMax=end,
                singleEvents=True, orderBy="startTime",
            ).execute()
        except Exception as exc:
            log("WARN", f"holiday sync failed for {cal_id}: {exc}")
            continue
        for ev in result.get("items", []):
            desc  = (ev.get("description") or "").lower()
            title = (ev.get("summary") or "").lower()
            if desc_kws and not all(k in desc for k in desc_kws):
                continue
            if title_kws and not all(k in title for k in title_kws):
                continue
            start = ev.get("start", {})
            d = start.get("date") or start.get("dateTime", "")[:10]
            if d:
                all_dates.add(d)
    path = CALENDAR / "holidays.json"
    path.write_text(json.dumps(sorted(all_dates)))
    log("OK", f"holidays: {len(all_dates)} public holidays cached")


def main():
    log("START", "sidecar sync initiated")
    try:
        today_seen:  dict = {}
        weekly_seen: dict = {}
        first_svc = None
        for m in aaka_config.auth_members():
            if not m.get("calendars"):
                # No calendars configured — skip event fetch (Gmail-only auth members
                # 403 on other members' calendar IDs and poison the skip_cache)
                continue
            try:
                svc = get_service_for(m["id"])
                if first_svc is None:
                    first_svc = svc
                _fetch_into(svc, 1, today_seen)
                _fetch_into(svc, 8, weekly_seen)
                log("INFO", f"[{m['id']}] sync pass complete")
            except json.JSONDecodeError as e:
                # Google API returned empty body — transient; retry once after a short wait
                import time as _time
                _time.sleep(5)
                try:
                    _fetch_into(svc, 1, today_seen)
                    _fetch_into(svc, 8, weekly_seen)
                    log("INFO", f"[{m['id']}] sync pass complete (retry ok)")
                except Exception as e2:
                    log("WARN", f"[{m['id']}] sync pass failed: {e2}")
            except Exception as e:
                log("WARN", f"[{m['id']}] sync pass failed: {e}")
        today_events  = sorted(today_seen.values(),  key=_ev_sort_key)
        weekly_events = sorted(weekly_seen.values(), key=_ev_sort_key)

        today_path  = CALENDAR / "today.md"
        weekly_path = CALENDAR / "weekly.md"

        write_markdown(today_events,  today_path,  "Today's Schedule")
        write_markdown(weekly_events, weekly_path, "This Week's Schedule")
        write_weekly_cache(weekly_events)
        write_member_events_cache(weekly_events)
        write_member_md_files(weekly_events)

        if first_svc:
            try:
                sync_holidays(first_svc)
            except Exception as e:
                log("WARN", f"holiday sync failed: {e}")

        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        snapshot(today_path)
        snapshot(weekly_path)

        log("OK", f"synced {len(today_events)} today / {len(weekly_events)} weekly events")

        # Tasks are now stored locally in data/tasks/tasks.json — no Google Tasks sync needed.

        # Contacts / birthday sync (CSV or People API).
        # On sensor role this is disabled via aaka.yaml roles.sensor.skills_disabled —
        # the skill returns {"skipped": ...} and we say nothing.
        try:
            from skills.contacts.contacts_sync import run as _contacts_sync
            _result = _contacts_sync()
            if "skipped" in _result:
                pass  # role policy — silent no-op
            elif "error" in _result:
                log("WARN", f"contacts sync failed: {_result['error']}")
            else:
                log("INFO", f"contacts: {_result['total']} contacts, {_result['in_window']} in 30-day window")
        except Exception as _ce:
            log("WARN", f"contacts sync error: {_ce}")

    except Exception as e:
        log("ERROR", f"sync failed: {e}")
        err_path = LOGS / "last_error.txt"
        err_path.write_text(traceback.format_exc())

if __name__ == "__main__":
    if "--check" in sys.argv:
        check_calendars()
    elif "--heartbeat" in sys.argv:
        import time
        t0 = time.time()
        get_service().calendarList().list(maxResults=1).execute()
        print(f"OK latency={int((time.time()-t0)*1000)}ms")
        sys.exit(0)
    else:
        main()