"""
Mail viewer — read-only Maildir reader for sensor-side chat commands.

Reads locally stored mail from Maildir directories written by fetch.py.
Never touches the network. Zero LLM tokens.
"""

import email as email_lib
import json
import mailbox
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

BASE = Path(
    os.environ.get("AAKA_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config

ACCOUNTS_CONFIG = Path("/Users/Shared/secrets/mail-fetch/accounts.yaml")
MAIL_DATA_DIR = aaka_config.DATA_DIR / "mail"


def _load_account_names() -> list[str]:
    if not ACCOUNTS_CONFIG.exists():
        return []
    with open(ACCOUNTS_CONFIG) as f:
        data = yaml.safe_load(f)
    return [a["name"] for a in (data or {}).get("accounts", []) if a.get("enabled", True)]


def _load_last_fetch(account_name: str) -> dict:
    p = MAIL_DATA_DIR / account_name / ".last_fetch.json"
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def _message_count(account_name: str) -> int:
    mdir_path = MAIL_DATA_DIR / account_name
    if not mdir_path.exists():
        return 0
    try:
        mdir = mailbox.Maildir(str(mdir_path), create=False)
        return len(mdir)
    except Exception:
        return 0


def _get_header(msg, header: str) -> str:
    val = msg.get(header, "")
    if not val:
        return ""
    # Decode RFC 2047 encoded words
    try:
        parts = email_lib.header.decode_header(val)
        decoded_parts = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded_parts.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded_parts.append(part)
        return " ".join(decoded_parts).strip()
    except Exception:
        return str(val).strip()


def _html_to_text(html: str) -> str:
    """Strip HTML tags for plain text display."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_body(msg) -> str:
    """Extract text/plain body, falling back to text/html."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace").strip()
        # No plain text found — try html
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
                return _html_to_text(html)
        return "(no text content)"
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload is None:
            return "(no content)"
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace").strip()
        if ct == "text/html":
            return _html_to_text(text)
        return text


def _list_messages_from_maildir(account_name: str) -> list[dict]:
    """Return all messages sorted by date descending."""
    mdir_path = MAIL_DATA_DIR / account_name
    if not mdir_path.exists():
        return []
    try:
        mdir = mailbox.Maildir(str(mdir_path), create=False)
    except Exception:
        return []

    messages = []
    for key, msg in mdir.items():
        date_str = _get_header(msg, "Date")
        messages.append({
            "key": key,
            "from": _get_header(msg, "From"),
            "subject": _get_header(msg, "Subject") or "(no subject)",
            "date": date_str,
        })

    # Sort newest first (string sort on ISO-ish dates is approximate but fine)
    messages.sort(key=lambda m: m["date"], reverse=True)
    return messages


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def list_accounts() -> str:
    """Summary of all accounts: name, message count, last fetch time."""
    names = _load_account_names()
    if not names:
        return "No mail accounts configured.\nEdit /Users/Shared/secrets/mail-fetch/accounts.yaml"

    lines = ["📬 *Mail accounts*\n"]
    for name in names:
        count = _message_count(name)
        lf = _load_last_fetch(name)
        if lf.get("timestamp"):
            ts = lf["timestamp"][:16].replace("T", " ")
            fetched = lf.get("count", 0)
            last = f"last fetch {ts} UTC (+{fetched})"
        else:
            last = "never fetched"
        lines.append(f"• *{name}* — {count} msgs, {last}")

    return "\n".join(lines)


def list_messages(account: str, n: int = 10) -> str:
    """List recent N messages for an account."""
    names = _load_account_names()
    if account not in names:
        close = [nm for nm in names if account.lower() in nm.lower()]
        if close:
            return f"Account {account!r} not found. Did you mean: {', '.join(close)}?"
        return f"Account {account!r} not found. Accounts: {', '.join(names)}"

    messages = _list_messages_from_maildir(account)
    if not messages:
        return f"No messages in {account}.\nRun `/mail fetch` to fetch mail."

    recent = messages[:n]
    lines = [f"📬 *{account}* — {len(messages)} total, showing {len(recent)}\n"]
    for i, m in enumerate(recent, 1):
        date = m["date"][:16] if len(m["date"]) >= 16 else m["date"]
        sender = m["from"]
        if len(sender) > 35:
            sender = sender[:32] + "…"
        subj = m["subject"]
        if len(subj) > 50:
            subj = subj[:47] + "…"
        lines.append(f"{i}. {date}  {sender}\n   {subj}")

    lines.append(f"\nRead: `/mail read {account} <N>`")
    return "\n".join(lines)


def read_message(account: str, n: int) -> str:
    """Read message number N (1-based, newest first) from account."""
    names = _load_account_names()
    if account not in names:
        return f"Account {account!r} not found."

    messages = _list_messages_from_maildir(account)
    if not messages:
        return f"No messages in {account}."

    if n < 1 or n > len(messages):
        return f"Message {n} not found. {account} has {len(messages)} messages."

    msg_info = messages[n - 1]
    mdir_path = MAIL_DATA_DIR / account
    mdir = mailbox.Maildir(str(mdir_path), create=False)
    msg = mdir[msg_info["key"]]

    from_hdr = _get_header(msg, "From")
    to_hdr = _get_header(msg, "To")
    subj = _get_header(msg, "Subject") or "(no subject)"
    date = _get_header(msg, "Date")
    body = _extract_body(msg)

    # Truncate very long bodies
    max_chars = 3000
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n… (truncated, {len(body)} chars total)"

    return (
        f"📧 *{subj}*\n"
        f"From: {from_hdr}\n"
        f"To: {to_hdr}\n"
        f"Date: {date}\n"
        f"─────────────────\n"
        f"{body}"
    )
