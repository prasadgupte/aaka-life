"""
Aaka — Send-to-Contact Skill

Channel-routing outbound send for external contacts (non-members).
Picks the right transport based on available contact info:
  WhatsApp — if phone present; uses gateway directly (works on VPS)
  Email    — if email present; uses Gmail API via member OAuth token (Mac executor)

Public API:
  send_to_contact(**kwargs) → {"sent": bool, "channel": str|None, "error": str|None}
"""

import base64
import email.mime.text
import json
import logging
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config

log = logging.getLogger("outbox.send_contact")


# ── WhatsApp ──────────────────────────────────────────────────────────────────

def _send_whatsapp(phone: str, message: str, *, dry_run: bool = False) -> dict:
    """Send a WhatsApp message to an arbitrary phone number via the gateway."""
    # Ensure E.164 leading +
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    if dry_run:
        log.info("[dry-run] WhatsApp → %s: %s", phone, message[:80])
        return {"sent": True, "channel": "whatsapp"}
    try:
        from gateway.adapter import GatewayAdapter
        GatewayAdapter().send_message("whatsapp", phone, message)
        return {"sent": True, "channel": "whatsapp"}
    except Exception as exc:
        log.warning("WhatsApp send failed to %s: %s", phone, exc)
        return {"sent": False, "channel": "whatsapp", "error": str(exc)}


# ── Signal ────────────────────────────────────────────────────────────────────

def _send_signal(phone: str, message: str, *, dry_run: bool = False) -> dict:
    """Send a Signal message to an arbitrary phone number via the egress gateway.

    Note this only reaches people who are actually on Signal — unlike WhatsApp
    there is no delivery-side fallback, so a non-Signal number simply errors.
    """
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    if dry_run:
        log.info("[dry-run] Signal → %s: %s", phone, message[:80])
        return {"sent": True, "channel": "signal"}
    try:
        from gateway.egress import MessageKind, OutboundMessage, send
        send(OutboundMessage(kind=MessageKind.TEXT, recipient=phone,
                             channel="signal", text=message, source="send_contact"))
        return {"sent": True, "channel": "signal"}
    except Exception as exc:
        log.warning("Signal send failed to %s: %s", phone, exc)
        return {"sent": False, "channel": "signal", "error": str(exc)}


def phone_channel() -> str:
    """Which enabled channel we use to reach a bare phone number.

    WhatsApp wins when it is enabled (it reaches almost anyone); Signal is used
    when it is the only messaging channel configured. Callers that only have an
    email address should pass channel="email" instead.
    """
    try:
        from gateway.config import ENABLED_CHANNELS
        enabled = [c.strip().lower() for c in ENABLED_CHANNELS]
    except Exception:
        enabled = []
    if "whatsapp" in enabled:
        return "whatsapp"
    if "signal" in enabled:
        return "signal"
    return "whatsapp"  # historical default when nothing is declared


# ── Email ─────────────────────────────────────────────────────────────────────

def _get_gmail_service(member_id: str):
    """Build Gmail API service for a member using their stored OAuth token."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_path = aaka_config.TOKENS_DIR / f"token_{member_id}.json"
    if not token_path.exists():
        token_path = aaka_config.TOKENS_DIR / "token.json"
    if not token_path.exists():
        raise FileNotFoundError(f"No token file for member {member_id!r}")

    creds_path = aaka_config.TOKENS_DIR / "credentials.json"
    client_data = json.loads(creds_path.read_text())
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


def _send_email(
    from_member_id: str,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    *,
    cc: "list[str] | str | None" = None,
    dry_run: bool = False,
) -> dict:
    """Send an email FROM a member's Gmail account TO an external email address."""
    cc_list = [cc] if isinstance(cc, str) else (cc or [])
    if dry_run:
        log.info("[dry-run] email → %s (%s) cc=%s: %s", to_email, to_name, cc_list, subject)
        return {"sent": True, "channel": "email"}
    try:
        service = _get_gmail_service(from_member_id)
        msg = email.mime.text.MIMEText(body, "plain")
        msg["to"] = f"{to_name} <{to_email}>" if to_name else to_email
        msg["from"] = "me"
        msg["subject"] = subject
        if cc_list:
            msg["cc"] = ", ".join(cc_list)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"sent": True, "channel": "email"}
    except Exception as exc:
        log.warning("Email send failed to %s: %s", to_email, exc)
        return {"sent": False, "channel": "email", "error": str(exc)}


# ── Public API ────────────────────────────────────────────────────────────────

def send_to_contact(
    *,
    name: str,
    first_name: str,
    phone: str = "",
    email: str = "",
    message: str,
    from_member_id: str = "",
    channel: str = "auto",
    cc: "list[str] | str | None" = None,
    dry_run: bool = False,
) -> dict:
    """
    Send a message to an external contact via the best available channel.

    channel:
      "auto"      — phone_channel() if phone present, else email if email present
      "whatsapp"  — WhatsApp only (fails gracefully if no phone)
      "signal"    — Signal only (fails gracefully if no phone)
      "email"     — Email only (fails gracefully if no email or no token)

    cc: one address string or list of addresses (email channel only).

    Returns: {"sent": bool, "channel": str | None, "error": str | None}
    """
    phone_ch  = phone_channel() if channel == "auto" else channel
    use_wa    = phone_ch == "whatsapp"
    use_sig   = phone_ch == "signal"
    use_email = channel in ("auto", "email")

    if use_wa and phone:
        return _send_whatsapp(phone, message, dry_run=dry_run)

    if use_sig and phone:
        return _send_signal(phone, message, dry_run=dry_run)

    if use_email and email:
        if not from_member_id:
            return {"sent": False, "channel": "email",
                    "error": "from_member_id required for email send"}
        subject = f"Happy Birthday, {first_name}! 🎂"
        body    = f"{message}\n"
        return _send_email(from_member_id, email, name, subject, body, cc=cc, dry_run=dry_run)

    # No usable channel
    channels_tried = []
    if use_wa:
        channels_tried.append("whatsapp (no phone)")
    if use_sig:
        channels_tried.append("signal (no phone)")
    if use_email:
        channels_tried.append("email (no address)")
    return {
        "sent": False,
        "channel": None,
        "error": f"No contact info for {name!r} — tried: {', '.join(channels_tried) or 'none'}",
    }
