#!/usr/bin/env python3
"""
Aaka — check_token_expiry.py
Nightly: verify every OAuth token in $AAKA_CONFIG_DIR/tokens/ can still
authenticate against Google APIs.

Tracks last-verified date per token. Sends a Telegram warning to admin
members at 1, 2, 3, 5, and 10 days without a successful verification —
giving progressively earlier reminders that a re-auth may be needed.

Called via cron (sensor/entrypoint.sh). Refreshes from executor are fine:
when executor runs gog.get_service() it refreshes tokens automatically,
so the last-verified file stays current.
"""

import sys, os, json, datetime
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE", "/app"))
sys.path.insert(0, str(BASE))
import aaka_config

TOKENS_DIR  = aaka_config.TOKENS_DIR
HEALTH_DIR  = aaka_config.LOGS_DIR / "token_health"
WARN_AT_DAYS = {1, 2, 3, 5, 10}


def _admin_sender_ids() -> list[str]:
    """Return sender IDs for members with admin role."""
    ids = []
    for m in aaka_config.members():
        if m.get("role") not in ("admin", "adult"):
            continue
        sid = m.get("telegram_id") or m.get("whatsapp_id") or m.get("sender_id", "")
        if sid:
            ids.append(sid)
    return ids


def _send_warning(text: str) -> None:
    try:
        from aaka_queue.queue import write_outbox
        for sid in _admin_sender_ids():
            write_outbox(channel_id=sid, sender=sid, text=text)
    except Exception as e:
        print(f"[token_expiry] could not write outbox: {e}")


def _check_token(token_path: Path) -> bool:
    """Return True if token can be refreshed and makes a successful API call."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    try:
        creds = Credentials.from_authorized_user_file(str(token_path))
        if not creds.refresh_token:
            return False
        if creds.expired:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        from googleapiclient.discovery import build
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        # Use events.list on primary calendar — works with both calendar.readonly
        # and calendar.events scopes (token_vps.json only has calendar.events).
        svc.events().list(calendarId="primary", maxResults=1).execute()
        return True
    except Exception as e:
        print(f"[token_expiry] {token_path.name} check failed: {e}")
        return False


def main():
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    from aaka_config import reply_prefix
    prefix = reply_prefix()

    token_files = sorted(TOKENS_DIR.glob("token_*.json"))
    if not token_files:
        print("[token_expiry] no token_*.json files found — skipping")
        return

    for token_path in token_files:
        name = token_path.stem  # e.g. "token_vps" or "token_aakash"
        health_file = HEALTH_DIR / f"{name}.last_ok"

        healthy = _check_token(token_path)

        if healthy:
            health_file.write_text(today.isoformat())
            print(f"[token_expiry] {name}: OK")
            continue

        # Token unhealthy — compute days without a successful check
        if health_file.exists():
            try:
                last_ok = datetime.date.fromisoformat(health_file.read_text().strip())
                days_without = (today - last_ok).days
            except Exception:
                days_without = 999
        else:
            days_without = 999

        label = "days" if days_without != 1 else "day"
        print(f"[token_expiry] {name}: UNHEALTHY (last OK: {days_without} {label} ago)")

        if days_without in WARN_AT_DAYS or days_without > max(WARN_AT_DAYS):
            msg = (
                f"{prefix}⚠️ Token health alert: *{name}*\n"
                f"Last verified: {days_without} {label} ago\n"
                f"Action: re-run auth on the Mac executor:\n"
                f"`python3 admin/reauth.py`\n"
                f"Then copy the new token to VPS if needed."
            )
            _send_warning(msg)


if __name__ == "__main__":
    main()
