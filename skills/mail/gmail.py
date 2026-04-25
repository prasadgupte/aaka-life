"""
Aaka — Gmail reader.

get_gmail_service(member_id=None)
  member_id=None  → uses bot account token (token_aakash.json / first auth_member)
  member_id="alex" → resolves via aaka_config.auth_for("alex") → token.json
"""

import base64
import email as email_lib
import os
import sys
from pathlib import Path
import re

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config


def get_gmail_service(member_id: str = ""):
    """Return an authenticated Gmail API service object.

    member_id: if set, uses the token from that member's auth block in aaka.yaml.
               If empty, falls back to the first auth_member, then token_aakash.json.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_path: Path | None = None

    if member_id:
        auth = aaka_config.auth_for(member_id)
        if auth:
            token_path = auth["token_file"]
    if token_path is None:
        # Fallback: first auth member, then bot token
        auth_ms = aaka_config.auth_members()
        if auth_ms:
            info = aaka_config.auth_for(auth_ms[0]["id"])
            if info:
                token_path = info["token_file"]
    if token_path is None:
        token_path = aaka_config.TOKENS_DIR / "token_aakash.json"

    if not token_path.exists():
        raise FileNotFoundError(
            f"Gmail token not found: {token_path}\n"
            f"Run: python3 admin/reauth.py {member_id or 'aakash'}"
        )
    creds = Credentials.from_authorized_user_file(str(token_path))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def create_label(
    svc,
    name: str,
    label_list_visibility: str = "labelShow",
    message_list_visibility: str = "show",
) -> dict:
    """Create a new Gmail label. Returns the created label dict.

    Requires gmail.labels scope.

    Args:
        name: Full label name (use "/" for nesting, e.g. "Travel/JP-2026").
        label_list_visibility: "labelShow" | "labelHide" | "labelShowIfUnread"
        message_list_visibility: "show" | "hide"

    Returns: {id, name, type, labelListVisibility, messageListVisibility, ...}
    Raises: googleapiclient.errors.HttpError if label already exists or auth fails.
    """
    body = {
        "name": name,
        "labelListVisibility": label_list_visibility,
        "messageListVisibility": message_list_visibility,
    }
    return svc.users().labels().create(userId="me", body=body).execute()


def list_labels(svc) -> list[dict]:
    """Return all Gmail labels [{id, name, type}] (system + user)."""
    result = svc.users().labels().list(userId="me").execute()
    return result.get("labels", [])


def apply_labels(
    svc,
    message_id: str,
    add_ids: list[str] | None = None,
    remove_ids: list[str] | None = None,
) -> dict:
    """Add and/or remove labels on a single message.

    Requires gmail.modify scope. Returns the updated message resource (id, labelIds).
    Pass system label IDs as-is ("INBOX", "UNREAD"); user labels by their id (Label_NNN).
    """
    body: dict = {}
    if add_ids:
        body["addLabelIds"] = add_ids
    if remove_ids:
        body["removeLabelIds"] = remove_ids
    return svc.users().messages().modify(
        userId="me", id=message_id, body=body,
    ).execute()


def parse_list_unsubscribe(headers: dict) -> str:
    """Extract a usable unsubscribe URL from List-Unsubscribe header.

    Header format (RFC 8058): "<https://...>, <mailto:...>" — angle-bracketed,
    comma-separated. Prefers https over mailto. Returns "" if none found.
    """
    raw = headers.get("List-Unsubscribe") or headers.get("list-unsubscribe") or ""
    if not raw:
        return ""
    candidates = re.findall(r"<([^>]+)>", raw)
    https = [c for c in candidates if c.lower().startswith(("http://", "https://"))]
    if https:
        return https[0]
    mailto = [c for c in candidates if c.lower().startswith("mailto:")]
    return mailto[0] if mailto else ""


def fetch_headers_full(svc, message_id: str) -> dict:
    """Fetch a message's full headers dict + snippet. Lighter than fetch_full
    when body isn't needed (uses metadata format)."""
    msg = svc.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date", "List-Unsubscribe"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "headers": headers,
        "snippet": msg.get("snippet", ""),
        "labelIds": msg.get("labelIds", []),
    }


def list_unread(max_results: int = 10) -> list[dict]:
    """Return up to max_results unread inbox messages.

    Each dict has: from_addr, subject, snippet, date.
    """
    svc = get_gmail_service()
    result = svc.users().messages().list(
        userId="me",
        q="is:unread in:inbox",
        maxResults=max_results,
    ).execute()

    messages = result.get("messages", [])
    out = []
    for m in messages:
        msg = svc.users().messages().get(
            userId="me",
            id=m["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        out.append({
            "from_addr": headers.get("From", ""),
            "subject":   headers.get("Subject", "(no subject)"),
            "snippet":   msg.get("snippet", ""),
            "date":      headers.get("Date", ""),
        })
    return out


def list_by_label(svc, label_name: str, max_results: int = 20) -> list[dict]:
    """Return up to max_results unread messages with the given Gmail label.

    Returns a list of minimal dicts with {"id": ..., "threadId": ...}.
    Use fetch_full() to get headers and body.
    """
    result = svc.users().messages().list(
        userId="me",
        q=f"label:{label_name}",
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def fetch_full(svc, message_id: str, max_chars: int = 3000) -> dict:
    """Fetch a full Gmail message, returning plain-text body.

    Body extraction priority:
      1. text/plain MIME part  (fewest tokens, no markup)
      2. text/html MIME part   → strip tags via stdlib html.parser
      3. snippet               (Gmail's 200-char preview, last resort)

    Body is truncated to max_chars before returning to bound LLM token cost.

    Returns: {from_addr, subject, date, body}
    """
    msg = svc.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()
    headers = {h["name"]: h["value"]
               for h in msg.get("payload", {}).get("headers", [])}
    body = (
        _extract_plain(msg.get("payload", {}))
        or _extract_html_as_text(msg.get("payload", {}))
        or msg.get("snippet", "")
    )
    return {
        "from_addr": headers.get("From", ""),
        "subject":   headers.get("Subject", "(no subject)"),
        "date":      headers.get("Date", ""),
        "body":      body[:max_chars],
    }


def fetch_attachments(svc, message_id: str) -> "list[dict]":
    """Fetch all attachments from a Gmail message.

    Returns a list of dicts:
        {filename: str, data: bytes, mime_type: str, size: int}

    Inline parts (no filename) are skipped. Large attachments are fetched via
    the attachments API; small inline-encoded ones are decoded from the body.
    """
    msg = svc.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()
    return _collect_attachments(svc, message_id, msg.get("payload", {}))


def _collect_attachments(svc, message_id: str, payload: dict) -> "list[dict]":
    """Recursively walk MIME parts and collect named attachments."""
    results = []
    filename = payload.get("filename", "")
    body = payload.get("body", {})

    if filename:
        attachment_id = body.get("attachmentId")
        if attachment_id:
            att = svc.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute()
            raw = base64.urlsafe_b64decode(att.get("data", "") + "==")
        else:
            raw_b64 = body.get("data", "")
            raw = base64.urlsafe_b64decode(raw_b64 + "==") if raw_b64 else b""
        results.append({
            "filename": filename,
            "data": raw,
            "mime_type": payload.get("mimeType", "application/octet-stream"),
            "size": len(raw),
        })

    for part in payload.get("parts", []):
        results.extend(_collect_attachments(svc, message_id, part))

    return results


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _extract_plain(payload: dict) -> str:
    """Recursively find and return the text/plain MIME part."""
    if payload.get("mimeType") == "text/plain":
        return _decode_part(payload)
    for part in payload.get("parts", []):
        result = _extract_plain(part)
        if result:
            return result
    return ""


def _extract_html_as_text(payload: dict) -> str:
    """Recursively find text/html MIME part and strip tags to visible text."""
    if payload.get("mimeType") == "text/html":
        raw = _decode_part(payload)
        return _html_to_text(raw) if raw else ""
    for part in payload.get("parts", []):
        result = _extract_html_as_text(part)
        if result:
            return result
    return ""


def _html_to_text(html_str: str) -> str:
    """Strip HTML to visible text using stdlib only (no external dependencies)."""
    import html as _html
    from html.parser import HTMLParser

    class _Extractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self._skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "head"):
                self._skip = True
            if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "head"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                self.parts.append(data)

    ex = _Extractor()
    ex.feed(html_str)
    text = _html.unescape("".join(ex.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def create_draft(
    svc,
    to: "list[str]",
    subject: str,
    body: str,
    *,
    html_body: str = "",
    cc: "list[str] | None" = None,
    bcc: "list[str] | None" = None,
    reply_to_message_id: str = "",
) -> dict:
    """Create a Gmail draft.  Returns {draft_id, message_id, thread_id}.

    to: list of raw email addresses (e.g. ["foo@example.com"])
    Requires gmail.compose or gmail.modify scope.

    If reply_to_message_id is set, the draft is threaded under that message.
    """
    import email.mime.multipart
    import email.mime.text

    if html_body:
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg.attach(email.mime.text.MIMEText(body, "plain"))
        msg.attach(email.mime.text.MIMEText(html_body, "html"))
    else:
        msg = email.mime.text.MIMEText(body, "plain")

    msg["to"] = ", ".join(to)
    msg["subject"] = subject
    if cc:
        msg["cc"] = ", ".join(cc)
    if bcc:
        msg["bcc"] = ", ".join(bcc)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft_body: dict = {"message": {"raw": raw}}

    if reply_to_message_id:
        thread_msg = (
            svc.users()
            .messages()
            .get(userId="me", id=reply_to_message_id, format="minimal")
            .execute()
        )
        draft_body["message"]["threadId"] = thread_msg.get("threadId", "")

    result = svc.users().drafts().create(userId="me", body=draft_body).execute()
    return {
        "draft_id": result.get("id", ""),
        "message_id": result.get("message", {}).get("id", ""),
        "thread_id": result.get("message", {}).get("threadId", ""),
    }


def send_email_to_member(
    to_member_id: str,
    subject: str,
    body: str,
    *,
    html_body: str = "",
    from_member_id: str = "aakash",
) -> dict:
    """Send an email from aakash's Gmail account to a registered family member.

    to_member_id: member id from aaka.yaml (e.g. "alex", "tsu", "kiran")
    Returns {"message_id": str, "to": str} on success.
    Raises ValueError if the member has no email address.
    Raises FileNotFoundError if the sender token is missing — run reauth.py aakash.
    Requires gmail.send scope on from_member_id's token.
    """
    import email.mime.multipart
    import email.mime.text

    to_member = aaka_config.member_by_name(to_member_id)
    if not to_member:
        raise ValueError(f"Unknown member: {to_member_id!r}")
    to_email = to_member.get("email", "")
    if not to_email:
        raise ValueError(f"Member {to_member_id!r} has no email address configured")

    svc = get_gmail_service(from_member_id)

    if html_body:
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg.attach(email.mime.text.MIMEText(body, "plain"))
        msg.attach(email.mime.text.MIMEText(html_body, "html"))
    else:
        msg = email.mime.text.MIMEText(body, "plain")

    msg["to"] = to_email
    msg["from"] = "me"
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": result.get("id", ""), "to": to_email}


def format_inbox_reply(messages: list[dict]) -> str:
    """Format unread messages in Aakash☁️ voice for WhatsApp."""
    n = len(messages)
    if n == 0:
        return "All clear ☁️ No new mail. Enjoy the peace!"
    lines = [f"Hey! ☁️ {n} unread message{'s' if n != 1 else ''} waiting:\n"]
    for i, m in enumerate(messages, 1):
        from_short = m["from_addr"].split("<")[0].strip() or m["from_addr"]
        lines.append(f"{i}. *{m['subject']}*\n   From: {from_short}\n   {m['snippet'][:80]}…")
    return "\n".join(lines)
