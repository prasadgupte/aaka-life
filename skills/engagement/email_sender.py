"""
Aaka — Email Fallback Sender

Uses the member's existing Gmail OAuth token (token_{member_id}.json in TOKENS_DIR)
to send a plain-text email via the Gmail API.

No third-party services — privacy-first.
Only used after 7+ days of silence on primary channels.
"""

import base64
import email.mime.text
import json
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config


def _get_gmail_service(member_id: str):
    """Build Gmail API service for a member using their stored OAuth token."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_path = aaka_config.TOKENS_DIR / f"token_{member_id}.json"
    if not token_path.exists():
        # Fallback to generic token.json
        token_path = aaka_config.TOKENS_DIR / "token.json"
    if not token_path.exists():
        raise FileNotFoundError(f"No token file for member {member_id}")

    creds_path = aaka_config.TOKENS_DIR / "credentials.json"
    with open(creds_path) as f:
        client_data = json.load(f)
    client_info = client_data.get("installed") or client_data.get("web", {})

    creds = Credentials(
        token=None,
        refresh_token=json.loads(token_path.read_text()).get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_info.get("client_id"),
        client_secret=client_info.get("client_secret"),
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    if not creds.valid:
        creds.refresh(Request())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(member_id: str, subject: str, body: str) -> bool:
    """Send a plain-text email to the member's email address.

    Returns True on success, False on any failure.
    """
    m_obj = aaka_config.member_by_name(member_id)
    if not m_obj:
        return False
    to_email = m_obj.get("email", "")
    if not to_email:
        return False

    try:
        service = _get_gmail_service(member_id)
        msg = email.mime.text.MIMEText(body, "plain")
        msg["to"] = to_email
        msg["from"] = "me"  # Gmail API uses "me" for authenticated user
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        return True
    except Exception as exc:
        import logging
        logging.getLogger("engagement.email").warning("email send failed: %s", exc)
        return False
