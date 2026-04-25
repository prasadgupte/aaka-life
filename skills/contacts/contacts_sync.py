#!/usr/bin/env python3
"""
Aaka — Contacts Sync

Reads birthday data from a Google Contacts CSV export (or People API when token
is available) and writes two local cache files:

  birthdays.json       — full data (Mac-only, never synced to VPS)
                         fields: name, first_name, birthday (YYYY-MM-DD), year,
                                 phones (list), mobile (best mobile, normalized)

  birthday_window.json — 30-day forward rolling window (synced to VPS sensor)
                         fields: name, first_name, month_day (MM-DD), mobile
                         No birth year, no extra phones — privacy-minimal.

Called from skills/calendar/sidecar_sync.py main() on the 30-min cron.
Also runnable standalone: python3 skills/contacts/contacts_sync.py
"""

import csv
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config

CONTACTS_DIR = aaka_config.DATA_DIR / "contacts"
BIRTHDAYS_JSON = CONTACTS_DIR / "birthdays.json"
WINDOW_JSON    = CONTACTS_DIR / "birthday_window.json"
CSV_PATH       = CONTACTS_DIR / "contacts.csv"

WINDOW_DAYS = 30


def _normalize_phone(raw: str) -> str:
    """Strip spaces, dashes, parens → E.164-ish string."""
    return re.sub(r"[\s\-\(\)]", "", raw.strip())


def _pick_mobile(row: dict) -> str:
    """Return best normalized phone: mobile-labeled first, then any."""
    # Prefer mobile/cell label
    for i in range(1, 6):
        label = row.get(f"Phone {i} - Label", "").lower()
        value = row.get(f"Phone {i} - Value", "").strip()
        if value and ("mobile" in label or "cell" in label):
            return _normalize_phone(value)
    # Fallback: first non-empty phone
    for i in range(1, 6):
        value = row.get(f"Phone {i} - Value", "").strip()
        if value:
            return _normalize_phone(value)
    return ""


def _all_phones(row: dict) -> list[str]:
    return [
        _normalize_phone(row.get(f"Phone {i} - Value", ""))
        for i in range(1, 6)
        if row.get(f"Phone {i} - Value", "").strip()
    ]


def _pick_email(row: dict) -> str:
    """Return first non-empty email address from CSV row."""
    for i in range(1, 4):
        value = row.get(f"E-mail {i} - Value", "").strip()
        if value:
            return value
    return ""


def _parse_birthday(raw: str) -> tuple[str | None, int | None]:
    """
    Return (YYYY-MM-DD or MM-DD, year or None).
    Handles: '1984-03-07', '--03-07' (year-less).
    """
    raw = raw.strip()
    if not raw:
        return None, None
    if raw.startswith("--"):
        # Year-less: --MM-DD
        md = raw[2:]  # MM-DD
        return md, None
    try:
        d = date.fromisoformat(raw)
        return raw, d.year
    except ValueError:
        return None, None


def _load_from_csv(path: Path) -> list[dict]:
    entries = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_bday = row.get("Birthday", "").strip()
            if not raw_bday:
                continue
            bday_str, year = _parse_birthday(raw_bday)
            if not bday_str:
                continue

            first = row.get("First Name", "").strip()
            last  = row.get("Last Name",  "").strip()
            name  = (first + " " + last).strip() or row.get("Organization Name", "").strip()
            if not name:
                continue

            mobile = _pick_mobile(row)
            phones = _all_phones(row)
            email  = _pick_email(row)

            entries.append({
                "name":       name,
                "first_name": first or name.split()[0],
                "birthday":   bday_str,   # YYYY-MM-DD or MM-DD
                "year":       year,        # int or None
                "phones":     phones,
                "mobile":     mobile,
                "email":      email,       # primary email or ""
            })
    return entries


def _load_from_people_api() -> list[dict] | None:
    """
    Attempt to load birthdays from Google People API using the first auth member's token.
    Returns None if no suitable token/scope is available (silent fallback to CSV).
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        # Find first member with a token that has people.readonly scope
        for m in aaka_config.auth_members():
            token_path = aaka_config.TOKENS_DIR / f"token_{m['id']}.json"
            if not token_path.exists():
                continue
            token_data = json.loads(token_path.read_text())
            scopes = token_data.get("scopes", [])
            has_people = any("people" in s for s in (scopes if isinstance(scopes, list) else [scopes]))
            if not has_people:
                continue

            creds_path = aaka_config.TOKENS_DIR / "credentials.json"
            client_data = json.loads(creds_path.read_text())
            client_info = client_data.get("installed") or client_data.get("web", {})
            creds = Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_info["client_id"],
                client_secret=client_info["client_secret"],
                scopes=["https://www.googleapis.com/auth/contacts.readonly"],
            )
            if not creds.valid:
                creds.refresh(Request())

            service = build("people", "v1", credentials=creds, cache_discovery=False)
            entries = []
            page_token = None
            while True:
                kwargs = {
                    "resourceName": "people/me",
                    "pageSize": 1000,
                    "personFields": "names,birthdays,phoneNumbers,emailAddresses",
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                result = service.people().connections().list(**kwargs).execute()
                for person in result.get("connections", []):
                    bdays = person.get("birthdays", [])
                    if not bdays:
                        continue
                    bday = bdays[0].get("date", {})
                    month = bday.get("month")
                    day   = bday.get("day")
                    year  = bday.get("year")
                    if not (month and day):
                        continue
                    bday_str = f"{year:04d}-{month:02d}-{day:02d}" if year else f"{month:02d}-{day:02d}"

                    names = person.get("names", [])
                    display = names[0].get("displayName", "") if names else ""
                    first   = names[0].get("givenName", "") if names else ""
                    if not display:
                        continue

                    phones_raw = person.get("phoneNumbers", [])
                    mobile = ""
                    all_phones = []
                    for ph in phones_raw:
                        val = re.sub(r"[\s\-\(\)]", "", ph.get("value", "").strip())
                        if val:
                            all_phones.append(val)
                            if "mobile" in ph.get("type", "").lower() and not mobile:
                                mobile = val
                    if not mobile and all_phones:
                        mobile = all_phones[0]

                    emails_raw = person.get("emailAddresses", [])
                    email = emails_raw[0].get("value", "").strip() if emails_raw else ""

                    entries.append({
                        "name":       display,
                        "first_name": first or display.split()[0],
                        "birthday":   bday_str,
                        "year":       year,
                        "phones":     all_phones,
                        "mobile":     mobile,
                        "email":      email,
                    })
                page_token = result.get("nextPageToken")
                if not page_token:
                    break
            return entries
    except Exception:
        pass
    return None


def _build_window(entries: list[dict], days: int = WINDOW_DAYS) -> list[dict]:
    """Build a rolling window of upcoming birthdays, stripping birth year."""
    today = date.today()
    window = []
    for e in entries:
        bday_str = e["birthday"]
        # Normalize to MM-DD
        if len(bday_str) == 10:  # YYYY-MM-DD
            md = bday_str[5:]  # MM-DD
        else:
            md = bday_str      # already MM-DD

        month, day = int(md[:2]), int(md[3:])
        try:
            bday_this = date(today.year, month, day)
        except ValueError:
            continue  # Feb 29 on non-leap year
        if bday_this < today:
            try:
                bday_this = date(today.year + 1, month, day)
            except ValueError:
                continue
        days_away = (bday_this - today).days
        if days_away <= days:
            window.append({
                "name":       e["name"],
                "first_name": e["first_name"],
                "month_day":  md,
                "mobile":     e["mobile"],
                "days_away":  days_away,
            })
    window.sort(key=lambda x: x["days_away"])
    return window


def run(source_csv: Path | None = None) -> dict:
    """
    Main entry: load contacts, write both cache files.
    Returns summary dict with counts.

    Returns {"skipped": "..."} when disabled by role policy
    (see roles.<role>.skills_disabled in aaka.yaml).
    """
    if not aaka_config.skill_enabled("contacts_sync"):
        return {"skipped": f"contacts_sync disabled on role={aaka_config.role()}"}

    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)

    # Try People API first; fall back to CSV
    entries = _load_from_people_api()
    source = "people_api"
    if entries is None:
        csv_path = source_csv or CSV_PATH
        if not csv_path.exists():
            return {"error": f"No CSV at {csv_path} and no People API token"}
        entries = _load_from_csv(csv_path)
        source = "csv"

    # Write full cache (Mac-only)
    BIRTHDAYS_JSON.write_text(json.dumps(entries, ensure_ascii=False, indent=2))

    # Write privacy-minimal window
    window = _build_window(entries)
    WINDOW_JSON.write_text(json.dumps(window, ensure_ascii=False, indent=2))

    return {
        "source":   source,
        "total":    len(entries),
        "in_window": len(window),
    }


if __name__ == "__main__":
    result = run()
    if "error" in result:
        print(f"ERROR: {result['error']}")
        sys.exit(1)
    print(
        f"✅ contacts sync: {result['total']} contacts "
        f"({result['in_window']} in 30-day window) "
        f"source={result['source']}"
    )
