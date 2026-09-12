#!/usr/bin/env python3
"""
Aaka — Agent Gateway API
Runs on port 18790, localhost only.

Allows registered agents to send messages to users via aaka's
Telegram/WhatsApp channels and poll for user replies.

Auth: X-Agent-Key header (SHA-256 hash checked against agent_registry).
"""
import asyncio
import base64
import json
import os
import shutil
import tempfile as _tempfile
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

BASE = Path(os.environ.get("AAKA_BASE", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(BASE))

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import aaka_config
from aaka_queue.queue import (
    authenticate_agent,
    write_outbox,
    write_item,
    update_status,
    create_reply_request,
    get_agent_reply,
    consume_agent_reply,
    expire_reply_requests,
    create_agent_job,
    list_agent_jobs,
    delete_agent_job,
    update_agent_job,
    get_due_jobs,
    mark_job_fired,
    record_agent_action,
    get_agent_action,
    delete_agent_action,
    write_approval_item,
    get_item_by_approval_id,
    set_pending_confirm,
    create_scheduled_message,
    get_scheduled_message,
    list_scheduled_messages,
    cancel_scheduled_message,
)


# ── Scheduler loop ───────────────────────────────────────────────────────────

async def _scheduler_loop():
    """Check for due jobs every 30 seconds and fire them into the queue."""
    while True:
        try:
            due = get_due_jobs()
            for job in due:
                payload = json.loads(job.get("payload") or "{}")
                payload.update({
                    "job_id": job["id"],
                    "agent_id": job["agent_id"],
                    "job_name": job["name"],
                })
                item_id = write_item(
                    intent="agent_job",
                    raw_message=f"job:{job['agent_id']}:{job['name']}",
                    sender=job["agent_id"],
                    channel_id="",
                    source="internal",
                    payload=payload,
                )
                update_status(item_id, "confirmed")
                mark_job_fired(job["id"])
        except Exception as exc:
            print(f"[scheduler] error: {exc}")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="Aaka Agent Gateway", version="1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8081", "http://localhost:19000", "http://localhost:19006"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Agent-Key"],
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_agent(x_agent_key: str = Header(...)) -> dict:
    """FastAPI dependency: validates X-Agent-Key, returns agent row."""
    agent = authenticate_agent(x_agent_key)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return agent


# ── Models ────────────────────────────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    text: str
    sender: Optional[str] = None           # Telegram chat ID or WA phone; uses default if absent
    channel_id: Optional[str] = None       # Overrides sender if set
    source: str = "telegram"
    await_reply: bool = False              # True → also creates agent_reply_requests row
    ttl_minutes: int = 1440               # How long to wait for reply (default 24h)
    correlation_id: Optional[str] = None  # Caller-chosen; auto-generated if absent
    reply_options: Optional[list[str]] = None  # e.g. ["Window","Aisle"] → inline buttons
    silent: bool = False
    reply_to_message_id: Optional[str] = None


class SendPhotoRequest(BaseModel):
    photo_b64: str                          # base64-encoded image bytes
    filename: str = "photo.jpg"            # original filename (used for MIME detection)
    caption: str = ""
    sender: Optional[str] = None
    channel_id: Optional[str] = None
    silent: bool = False
    reply_options: Optional[list[str]] = None


class SendPhotoResponse(BaseModel):
    ok: bool
    message_id: Optional[int] = None


class SendMessageResponse(BaseModel):
    outbox_item_id: str
    correlation_id: Optional[str] = None


class ReplyResponse(BaseModel):
    correlation_id: str
    text: str
    received_at: str
    reply_id: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _default_recipient() -> tuple[str, str]:
    """Return (sender, channel_id) for the first configured family member with a Telegram ID."""
    try:
        members = aaka_config.members()
    except Exception:
        raise HTTPException(status_code=500, detail="Could not load aaka config")
    for m in members:
        tg = m.get("telegram")
        if tg:
            return str(tg), str(tg)
    raise HTTPException(status_code=500, detail="No default Telegram recipient in aaka.yaml")


def _build_reply_markup(options: list[str]) -> dict:
    """Build Telegram InlineKeyboardMarkup from a list of option strings (max 3 per row)."""
    buttons = [{"text": opt, "callback_data": opt} for opt in options]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return {"inline_keyboard": rows}


def _tg_bot_token() -> str:
    """Load Telegram bot token via the shared channel adapter (avoids duplication)."""
    from gateway.channels.telegram import bot_token
    return bot_token()


# ── Per-agent rate limiting (Phase 5B) ────────────────────────────────────────
# Sliding window: rate_limit_per_hour reads from agent_registry column (default 60).

_agent_rate_buckets: "dict[str, list[float]]" = {}
_AGENT_RATE_DEFAULT = 60


def _agent_rate_limit_per_hour(agent_id: str) -> int:
    """Read rate_limit_per_hour from agent_registry. Returns default if not set."""
    try:
        from aaka_queue.queue import _connect
        row = _connect().execute(
            "SELECT rate_limit_per_hour FROM agent_registry WHERE id=?", (agent_id,)
        ).fetchone()
        if row and row["rate_limit_per_hour"] is not None:
            return int(row["rate_limit_per_hour"])
    except Exception:
        pass
    return _AGENT_RATE_DEFAULT


def _check_agent_rate_limit(agent_id: str) -> tuple[bool, int]:
    """Check per-agent rate limit. Returns (allowed, retry_after_seconds)."""
    limit = _agent_rate_limit_per_hour(agent_id)
    now = time.time()
    window_start = now - 3600.0
    bucket = [t for t in _agent_rate_buckets.get(agent_id, []) if t >= window_start]
    _agent_rate_buckets[agent_id] = bucket
    if len(bucket) >= limit:
        oldest = bucket[0] if bucket else now
        retry_after = max(1, int(3600 - (now - oldest)))
        return False, retry_after
    bucket.append(now)
    _agent_rate_buckets[agent_id] = bucket
    return True, 0


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/v1/messages", response_model=SendMessageResponse, status_code=201)
def send_message(body: SendMessageRequest, agent: dict = Depends(_require_agent)):
    """Send a message to the user via aaka's outbox. Optionally register for a reply."""
    expire_reply_requests()  # housekeeping

    # Per-agent rate limit check
    allowed, retry_after = _check_agent_rate_limit(agent["id"])
    if not allowed:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )

    sender = body.sender
    channel_id = body.channel_id
    if not sender:
        sender, channel_id = _default_recipient()
    if not channel_id:
        channel_id = sender

    markup = None
    if body.reply_options:
        markup = _build_reply_markup(body.reply_options)

    # Ingress audit — agent-injected message
    try:
        from gateway.ingress import InboundMessage as _IM, Channel as _Ch, receive as _ingress_receive
        _ingress_receive(_IM(
            raw_text=body.text,
            sender_id=f"agent:{agent['id']}",
            channel=_Ch.AGENT,
            channel_id=channel_id,
            source=f"agent_api:{agent['id']}",
        ))
    except Exception:
        pass  # non-fatal

    outbox_id = write_outbox(
        channel_id=channel_id,
        sender=sender,
        text=body.text,
        reply_to_message_id=body.reply_to_message_id,
        source=body.source,
        silent=body.silent,
        reply_markup=markup,
    )

    corr_id = None
    if body.await_reply:
        corr_id = body.correlation_id or str(uuid.uuid4())
        create_reply_request(
            correlation_id=corr_id,
            agent_id=agent["id"],
            outbox_item_id=outbox_id,
            sender=sender,
            channel_id=channel_id,
            ttl_minutes=body.ttl_minutes,
        )

    return SendMessageResponse(outbox_item_id=outbox_id, correlation_id=corr_id)


class NotifyRequest(BaseModel):
    text: str
    silent: bool = False


class NotifyResponse(BaseModel):
    outbox_item_id: str
    chat_id: str


def _source_for_chat(chat_id: str) -> str:
    """Infer the channel source from a chat_id shape."""
    if chat_id.startswith("web:") or chat_id.startswith("web/"):
        return "web"
    if chat_id.startswith("whatsapp:") or chat_id.endswith("@s.whatsapp.net") or chat_id.endswith("@g.us"):
        return "whatsapp"
    if chat_id.startswith("signal:") or chat_id.startswith("group:"):
        return "signal"
    return "telegram"


@app.post("/v1/notify", response_model=NotifyResponse, status_code=201)
def notify(body: NotifyRequest, agent: dict = Depends(_require_agent)):
    """Post a notification to the configured notifications group.

    Group is configured in aaka.yaml under groups.<name> with
    purpose='notifications' and allow_agent_post=true. Returns 503 if
    no such group is configured.
    """
    expire_reply_requests()
    allowed, retry_after = _check_agent_rate_limit(agent["id"])
    if not allowed:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )

    import aaka_config
    grp = aaka_config.notifications_group()
    if not grp:
        raise HTTPException(503, "No notifications group configured")
    if not grp.get("allow_agent_post", True):
        raise HTTPException(403, f"Agent posting not allowed for group '{grp['id']}'")
    chat_ids = grp.get("chat_ids") or []
    if not chat_ids:
        raise HTTPException(503, f"Notifications group '{grp['id']}' has no chat_ids")

    # Strip optional channel prefix ("telegram:-100...", "web:notes" stays).
    chat_id = chat_ids[0]
    if ":" in chat_id and chat_id.split(":", 1)[0] in ("telegram", "whatsapp", "signal"):
        chat_id = chat_id.split(":", 1)[1]

    source = _source_for_chat(chat_id)
    outbox_id = write_outbox(
        channel_id=chat_id,
        sender=f"agent:{agent['id']}",
        text=f"[{agent['id']}] {body.text}",
        source=source,
        silent=body.silent,
    )
    return NotifyResponse(outbox_item_id=outbox_id, chat_id=chat_id)


@app.get("/v1/replies/{correlation_id}", response_model=ReplyResponse)
def get_reply(correlation_id: str, agent: dict = Depends(_require_agent)):
    """Poll for a user reply. Returns 404 if no reply yet."""
    reply = get_agent_reply(correlation_id)
    if not reply:
        raise HTTPException(status_code=404, detail="No reply yet")
    if reply["agent_id"] != agent["id"]:
        raise HTTPException(status_code=403, detail="Not your correlation_id")
    return ReplyResponse(
        correlation_id=correlation_id,
        text=reply["text"],
        received_at=reply["received_at"],
        reply_id=reply["id"],
    )


@app.delete("/v1/replies/{correlation_id}", status_code=204)
def delete_reply(correlation_id: str, agent: dict = Depends(_require_agent)):
    """Mark reply as consumed (idempotent)."""
    reply = get_agent_reply(correlation_id)
    if reply and reply["agent_id"] != agent["id"]:
        raise HTTPException(status_code=403, detail="Not your correlation_id")
    consume_agent_reply(correlation_id)


@app.get("/v1/gmail/emails")
def fetch_gmail_emails(
    label: str,
    member_id: str = "",
    max_results: int = 20,
    agent: dict = Depends(_require_agent),
):
    """Fetch emails with the given Gmail label. Returns up to max_results.

    No server-side seen tracking — caller manages that locally (e.g. messages/seen.json).
    Requires gmail.readonly scope on the resolved member token.

    Returns: {emails: [{id, subject, from_addr, date, body}], count: N}
    """
    from skills.mail.gmail import get_gmail_service, list_by_label, fetch_full
    try:
        svc = get_gmail_service(member_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        msgs = list_by_label(svc, label, max_results=max_results)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gmail API error: {exc}")
    emails = []
    for m in msgs:
        try:
            full = fetch_full(svc, m["id"])
            emails.append({"id": m["id"], **full})
        except Exception:
            continue
    return {"emails": emails, "count": len(emails)}


@app.get("/v1/gmail/attachments")
def fetch_gmail_attachments(
    id: str,                                            # Gmail message id
    member_id: str = "",
    include_data: bool = False,                          # list mode by default; set true to inline base64
    agent: dict = Depends(_require_agent),
):
    """List or fetch attachments on a Gmail message.

    Default (`include_data=false`): returns the attachment list with metadata only —
    `[{filename, mime_type, size}, ...]`. Use this to decide which attachments are worth
    pulling.

    With `include_data=true`: also includes `data_base64` per attachment. Same shape, plus
    the bytes URL-base64-encoded. Use sparingly — large PDFs balloon the response.

    Requires gmail.readonly scope on the resolved member token.

    Returns: {attachments: [{filename, mime_type, size, data_base64?}], count: N}
    """
    import base64
    from skills.mail.gmail import get_gmail_service, fetch_attachments
    try:
        svc = get_gmail_service(member_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        atts = fetch_attachments(svc, id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gmail API error: {exc}")
    out = []
    for a in atts:
        entry = {
            "filename": a.get("filename", ""),
            "mime_type": a.get("mime_type", "application/octet-stream"),
            "size": a.get("size", 0),
        }
        if include_data:
            entry["data_base64"] = base64.b64encode(a.get("data", b"")).decode("ascii")
        out.append(entry)
    return {"attachments": out, "count": len(out)}


class CreateLabelRequest(BaseModel):
    name: str                                           # e.g. "Travel/JP-2026"
    member_id: str = ""
    label_list_visibility: str = "labelShow"            # labelShow | labelHide | labelShowIfUnread
    message_list_visibility: str = "show"               # show | hide


class CreateLabelResponse(BaseModel):
    id: str
    name: str
    type: Optional[str] = None


@app.post("/v1/gmail/labels", response_model=CreateLabelResponse, status_code=201)
def create_gmail_label(body: CreateLabelRequest, agent: dict = Depends(_require_agent)):
    """Create a new Gmail label. Requires gmail.labels scope on the resolved member token.

    Use "/" in the name for nested labels, e.g. "Travel/JP-2026".
    Returns {id, name, type}.
    """
    from skills.mail.gmail import get_gmail_service, create_label
    try:
        svc = get_gmail_service(body.member_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        label = create_label(
            svc,
            name=body.name,
            label_list_visibility=body.label_list_visibility,
            message_list_visibility=body.message_list_visibility,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gmail API error: {exc}")
    return CreateLabelResponse(id=label["id"], name=label["name"], type=label.get("type"))


class CreateDraftRequest(BaseModel):
    to: list[str]                                           # raw email addresses
    subject: str
    body: str                                               # plain-text body
    html_body: str = ""                                     # optional HTML alternative
    cc: list[str] = []
    bcc: list[str] = []
    reply_to_message_id: str = ""                          # thread under this Gmail message id


class CreateDraftResponse(BaseModel):
    draft_id: str
    message_id: str
    thread_id: str


@app.post("/v1/gmail/drafts", response_model=CreateDraftResponse, status_code=201)
def create_gmail_draft(body: CreateDraftRequest, agent: dict = Depends(_require_agent)):
    """Create a Gmail draft in the admin (primary) account.

    All drafts are created via the primary (admin) token — member selection is not
    exposed because only one account is provisioned for draft creation.

    to/cc/bcc: raw email addresses, e.g. ["foo@example.com"].
    reply_to_message_id: if set, the draft is threaded under that Gmail message.

    Requires gmail.modify scope on the primary member's token.
    Returns {draft_id, message_id, thread_id}.
    """
    from skills.mail.gmail import get_gmail_service, create_draft
    try:
        svc = get_gmail_service("")  # resolves to first auth_member (admin)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        result = create_draft(
            svc,
            to=body.to,
            subject=body.subject,
            body=body.body,
            html_body=body.html_body,
            cc=body.cc or None,
            bcc=body.bcc or None,
            reply_to_message_id=body.reply_to_message_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gmail API error: {exc}")
    return CreateDraftResponse(**result)


# ── Scheduled outbound messages ───────────────────────────────────────────────

_VALID_CHANNELS = {"email", "telegram", "whatsapp", "signal"}
_MAX_FUTURE_DAYS = 90
_MAX_PAST_MINUTES = 5


def _parse_schedule_at(schedule_at: "str | None") -> str:
    """Parse and validate schedule_at.  Returns UTC ISO8601 string.

    Raises HTTPException(422) for naive datetimes, out-of-range values.
    If schedule_at is None, returns now UTC.
    """
    if not schedule_at:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Must carry explicit timezone offset
    try:
        from datetime import timezone as _tz
        import re as _re
        # Reject naive strings (no Z, no +HH:MM, no -HH:MM)
        if not _re.search(r"(Z|[+-]\d{2}:\d{2})$", schedule_at.strip()):
            raise ValueError("naive datetime")
        # Python 3.11+ fromisoformat handles Z; 3.9/3.10 need replace
        dt = datetime.fromisoformat(schedule_at.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid schedule_at: {exc}")

    now = datetime.now(timezone.utc)
    if dt < now - timedelta(minutes=_MAX_PAST_MINUTES):
        raise HTTPException(status_code=422, detail="schedule_at is too far in the past (>5 min)")
    if dt > now + timedelta(days=_MAX_FUTURE_DAYS):
        raise HTTPException(status_code=422, detail=f"schedule_at is more than {_MAX_FUTURE_DAYS} days out")

    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_email_payload(payload: dict) -> None:
    """Validate email-channel payload; raises HTTPException(422) on error."""
    to_id = payload.get("to_member_id", "")
    if not to_id:
        raise HTTPException(status_code=422, detail="email payload requires to_member_id")
    member = aaka_config.member_by_name(to_id)
    if not member:
        raise HTTPException(status_code=422, detail=f"Unknown member: {to_id!r}")
    if not member.get("email"):
        raise HTTPException(status_code=422, detail=f"Member {to_id!r} has no email address configured")
    from_id = payload.get("from_member_id", "aakash")
    if from_id != "aakash":
        raise HTTPException(status_code=422, detail="Only from_member_id='aakash' is supported (only token on VPS)")
    if not payload.get("subject"):
        raise HTTPException(status_code=422, detail="email payload requires subject")
    if not payload.get("body"):
        raise HTTPException(status_code=422, detail="email payload requires body")


def _validate_telegram_payload(payload: dict) -> None:
    if not payload.get("channel_id") and not payload.get("recipient"):
        raise HTTPException(status_code=422, detail="telegram payload requires channel_id or recipient")
    if not payload.get("text"):
        raise HTTPException(status_code=422, detail="telegram payload requires text")


def _validate_whatsapp_payload(payload: dict) -> None:
    if not payload.get("channel_id") and not payload.get("recipient"):
        raise HTTPException(status_code=422, detail="whatsapp payload requires channel_id or recipient")
    if not payload.get("text"):
        raise HTTPException(status_code=422, detail="whatsapp payload requires text")


def _validate_signal_payload(payload: dict) -> None:
    # recipient is "+E.164", a Signal uuid, or "group:<base64 groupId>".
    if not payload.get("channel_id") and not payload.get("recipient"):
        raise HTTPException(status_code=422, detail="signal payload requires channel_id or recipient")
    if not payload.get("text"):
        raise HTTPException(status_code=422, detail="signal payload requires text")


_PAYLOAD_VALIDATORS = {
    "email": _validate_email_payload,
    "telegram": _validate_telegram_payload,
    "whatsapp": _validate_whatsapp_payload,
    "signal": _validate_signal_payload,
}


class ScheduledMessageRequest(BaseModel):
    channel: str                               # 'email' | 'telegram' | 'whatsapp' | 'signal'
    payload: dict                              # channel-specific (see docs)
    schedule_at: Optional[str] = None         # ISO8601 with explicit TZ; default = now


class ScheduledMessageResponse(BaseModel):
    id: str
    status: str
    scheduled_at_utc: str
    channel: str


@app.post("/v1/scheduled/messages", response_model=ScheduledMessageResponse, status_code=201)
def schedule_message(body: ScheduledMessageRequest, agent: dict = Depends(_require_agent)):
    """Schedule an outbound message on any supported channel.

    Channels: 'email' | 'telegram' | 'whatsapp'.

    Email payload:   {to_member_id, subject, body, html_body?, from_member_id?}
    Telegram payload: {channel_id (or recipient), text, silent?, reply_markup?}
    WhatsApp payload: {channel_id (or recipient), text}

    schedule_at: ISO8601 with explicit TZ (e.g. "2026-06-01T09:00:00+02:00").
    Omit or pass null to schedule immediately (fires within ~60s on VPS).
    Returns {id, status, scheduled_at_utc, channel}.
    """
    allowed, retry_after = _check_agent_rate_limit(agent["id"])
    if not allowed:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )

    if body.channel not in _VALID_CHANNELS:
        raise HTTPException(status_code=422, detail=f"channel must be one of {sorted(_VALID_CHANNELS)}")

    _PAYLOAD_VALIDATORS[body.channel](body.payload)
    scheduled_at_utc = _parse_schedule_at(body.schedule_at)

    msg_id = create_scheduled_message(
        agent_id=agent["id"],
        channel=body.channel,
        payload=body.payload,
        scheduled_at_utc=scheduled_at_utc,
    )
    return ScheduledMessageResponse(
        id=msg_id, status="pending", scheduled_at_utc=scheduled_at_utc, channel=body.channel,
    )


@app.get("/v1/scheduled/messages/{msg_id}", response_model=dict)
def get_scheduled_msg(msg_id: str, agent: dict = Depends(_require_agent)):
    """Get status of a scheduled message. Returns 404 if not found or not owned by this agent."""
    row = get_scheduled_message(msg_id)
    if not row or row["agent_id"] != agent["id"]:
        raise HTTPException(status_code=404, detail="Scheduled message not found")
    return row


@app.get("/v1/scheduled/messages", response_model=list)
def list_scheduled_msgs(
    status: Optional[str] = None,
    agent: dict = Depends(_require_agent),
):
    """List scheduled messages for this agent. Filter by status= pending|sent|cancelled|error."""
    return list_scheduled_messages(agent["id"], status=status)


@app.delete("/v1/scheduled/messages/{msg_id}", status_code=204)
def cancel_scheduled_msg(msg_id: str, agent: dict = Depends(_require_agent)):
    """Cancel a pending scheduled message. No-op if already sent/cancelled (idempotent)."""
    row = get_scheduled_message(msg_id)
    if not row or row["agent_id"] != agent["id"]:
        raise HTTPException(status_code=404, detail="Scheduled message not found")
    cancel_scheduled_message(msg_id, agent["id"])


# ── Email send — thin wrapper over scheduled_messages ────────────────────────


class SendEmailRequest(BaseModel):
    to_member_id: str                          # member id from aaka.yaml (e.g. "alex", "tsu")
    subject: str
    body: str                                  # plain-text body
    html_body: Optional[str] = None            # optional HTML alternative
    from_member_id: str = "aakash"             # only 'aakash' supported (only VPS token)
    schedule_at: Optional[str] = None          # ISO8601 with TZ; omit = send within ~60s


class SendEmailResponse(BaseModel):
    id: str
    status: str
    scheduled_at_utc: str
    to: str


@app.post("/v1/email/send", response_model=SendEmailResponse, status_code=201)
def send_email(body: SendEmailRequest, agent: dict = Depends(_require_agent)):
    """Queue an email from aakash's Gmail to a registered family member.

    Sent via the always-on VPS sensor (sensor/scheduled_sender.py cron).
    schedule_at: ISO8601 with TZ. Omit for near-immediate delivery (~60s).
    Returns {id, status, scheduled_at_utc, to}.
    """
    allowed, retry_after = _check_agent_rate_limit(agent["id"])
    if not allowed:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )

    payload = {
        "to_member_id": body.to_member_id,
        "subject": body.subject,
        "body": body.body,
        "html_body": body.html_body or "",
        "from_member_id": body.from_member_id,
    }
    _validate_email_payload(payload)
    scheduled_at_utc = _parse_schedule_at(body.schedule_at)

    msg_id = create_scheduled_message(
        agent_id=agent["id"],
        channel="email",
        payload=payload,
        scheduled_at_utc=scheduled_at_utc,
    )

    member = aaka_config.member_by_name(body.to_member_id)
    to_email = member.get("email", "") if member else ""

    return SendEmailResponse(
        id=msg_id, status="pending", scheduled_at_utc=scheduled_at_utc, to=to_email,
    )


# ── Calendar events ───────────────────────────────────────────────────────────


class EventOccurrence(BaseModel):
    date: str                               # YYYY-MM-DD
    start_time: Optional[str] = None        # HH:MM (24h) — omit for all-day
    end_time: Optional[str] = None          # HH:MM (24h)
    end_date: Optional[str] = None          # YYYY-MM-DD for multi-day


class AddEventRequest(BaseModel):
    title: str
    occurrences: list[EventOccurrence]      # at least one
    description: Optional[str] = None
    location: Optional[str] = None
    member_id: str = ""                     # whose calendar (resolves calendar_id)
    calendar_tag: str = ""                  # e.g. "kids" — overrides member_id resolution
    guests: Optional[list[str]] = None      # email addresses
    private: bool = False
    timezone: Optional[str] = None


class AddEventResponse(BaseModel):
    event_ids: list[str]
    count: int
    action_id: str


@app.post("/v1/calendar/events", response_model=AddEventResponse, status_code=201)
def add_calendar_event(body: AddEventRequest, agent: dict = Depends(_require_agent)):
    """Add one or more calendar events to Google Calendar.

    Occurrences with start_time/end_time create timed events; without, all-day events.
    A silent Telegram notification is sent to the user after creation.
    """
    from skills.calendar.add_event import write_event

    payload = {
        "title": body.title,
        "occurrences": [occ.model_dump(exclude_none=True) for occ in body.occurrences],
        "description": body.description or "",
        "location": body.location or "",
        "sender_member_id": body.member_id,
        "calendar_tag": body.calendar_tag,
        "guests": body.guests or [],
        "private": body.private,
        "timezone": body.timezone or "",
        "no_email": not body.guests,
    }

    try:
        result = write_event(payload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Calendar write failed: {exc}")

    # Extract clean string IDs (write_event returns dicts {"id": ..., "link": ...})
    clean_ids = [
        e["id"] if isinstance(e, dict) else e for e in result["event_ids"]
    ]

    # Record in action ledger for later delete()
    cal_id = aaka_config.target_calendar_id(body.member_id, body.calendar_tag)
    action_id = record_agent_action(
        agent["id"], "calendar_event", clean_ids, calendar_id=cal_id,
    )

    # Silent notification to user
    _notify_agent_action(
        agent,
        f"📅 *{agent['display_name']}* added {result['count']} event(s): {body.title}",
    )

    return AddEventResponse(event_ids=clean_ids, count=result["count"], action_id=action_id)


# ── Tasks ────────────────────────────────────────────────────────────────────


class AddTaskRequest(BaseModel):
    title: str
    owner: str = ""                         # e.g. "alex"
    due_date: Optional[str] = None          # YYYY-MM-DD
    urgent: bool = False
    starred: bool = False
    tags: Optional[list[str]] = None        # e.g. ["#travel"]
    description: Optional[str] = None
    recurring: bool = False
    # Provenance (all optional — set by agents for traceability)
    project: Optional[str] = None           # entity/project slug, e.g. "japan26", "flo172"
    skill: Optional[str] = None             # skill path, e.g. "book_flight", "file_document"


class AddTaskResponse(BaseModel):
    task_id: str
    title: str
    due_date: Optional[str] = None
    action_id: str
    project: Optional[str] = None
    agent: Optional[str] = None
    skill: Optional[str] = None


@app.get("/v1/tasks")
def list_tasks(
    project: str = "",
    skill: str = "",
    owner: str = "",
    status: str = "open",
    tag: "list[str] | None" = None,
    title_prefix: str = "",
    limit: int = 200,
    agent: dict = Depends(_require_agent),
):
    """List tasks created by this agent.

    Filters (all optional):
      project      — exact match on project field
      skill        — exact match on skill field
      owner        — exact match on owner field
      status       — open (default) | done | deleted | all
      tag          — match if value is in task's tags list (repeatable: ?tag=a&tag=b)
      title_prefix — case-sensitive prefix match on title
      limit        — max rows returned (default 200, hard cap 1000)

    Always scoped to the calling agent. Cross-agent reads are not supported.
    Returns {"tasks": [...], "count": N}.
    """
    from skills.tasks.local_tasks import _load

    limit = min(max(1, limit), 1000)
    agent_id = agent["id"]

    all_tasks = _load()

    # Filter by agent (always scoped)
    tasks = [t for t in all_tasks if t.get("agent") == agent_id]

    # Filter by status
    if status != "all":
        tasks = [t for t in tasks if t.get("status", "open") == status]

    # Optional filters
    if project:
        tasks = [t for t in tasks if t.get("project") == project]
    if skill:
        tasks = [t for t in tasks if t.get("skill") == skill]
    if owner:
        tasks = [t for t in tasks if t.get("owner") == owner]
    if tag:
        tag_set = set(tag) if isinstance(tag, list) else {tag}
        tasks = [t for t in tasks if tag_set.intersection(t.get("tags") or [])]
    if title_prefix:
        tasks = [t for t in tasks if t.get("title", "").startswith(title_prefix)]

    tasks = tasks[:limit]

    def _to_response(t: dict) -> dict:
        priority = t.get("priority", "normal")
        return {
            "id":          t["id"],
            "title":       t.get("title", ""),
            "owner":       t.get("owner", ""),
            "due_date":    t.get("due_date") or None,
            "urgent":      priority == "high",
            "starred":     priority == "medium",
            "tags":        t.get("tags") or [],
            "description": t.get("description", ""),
            "recurring":   t.get("recurring", False),
            "project":     t.get("project") or None,
            "skill":       t.get("skill") or None,
            "agent":       t.get("agent") or None,
            "status":      t.get("status", "open"),
            "created_at":  t.get("created_at") or None,
            "updated_at":  t.get("updated_at") or None,
        }

    result = [_to_response(t) for t in tasks]
    return {"tasks": result, "count": len(result)}


@app.post("/v1/tasks", response_model=AddTaskResponse, status_code=201)
def add_task(body: AddTaskRequest, agent: dict = Depends(_require_agent)):
    """Add a task to the local task store.

    A silent Telegram notification is sent to the user after creation.
    """
    from skills.tasks.local_tasks import add_task as _add_task

    payload = body.model_dump(exclude_none=True)
    # Tag with agent source
    tags = payload.get("tags", []) or []
    tags.append(f"#agent:{agent['id']}")
    payload["tags"] = tags
    # Provenance: agent is always the authenticated caller
    payload["agent"] = agent["id"]

    try:
        task = _add_task(payload)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Task creation failed: {exc}")

    # Record in action ledger for later delete()
    action_id = record_agent_action(agent["id"], "task", [task["id"]])

    # Silent notification to user
    due_str = f" (due {task.get('due_date', '')})" if task.get("due_date") else ""
    _notify_agent_action(
        agent,
        f"✅ *{agent['display_name']}* added task: {task['title']}{due_str}",
    )

    return AddTaskResponse(
        task_id=task["id"],
        title=task["title"],
        due_date=task.get("due_date"),
        action_id=action_id,
        project=task.get("project"),
        agent=task.get("agent"),
        skill=task.get("skill"),
    )


class UpdateTaskRequest(BaseModel):
    title: Optional[str] = None
    owner: Optional[str] = None
    due_date: Optional[str] = None
    urgent: Optional[bool] = None
    starred: Optional[bool] = None
    tags: Optional[list[str]] = None
    description: Optional[str] = None
    recurring: Optional[bool] = None
    project: Optional[str] = None
    skill: Optional[str] = None
    status: Optional[str] = None   # "open" | "done"


@app.patch("/v1/tasks/{task_id}", status_code=200)
def patch_task(task_id: str, body: UpdateTaskRequest,
               agent: dict = Depends(_require_agent)):
    """Update any fields on an existing task by ID."""
    from skills.tasks.local_tasks import update_task_by_id, complete_task_by_id, delete_task

    updates = {k: v for k, v in body.model_dump().items() if v is not None}

    # Map urgent/starred → priority
    if "urgent" in updates:
        updates["priority"] = "high" if updates.pop("urgent") else updates.get("priority", "normal")
    if "starred" in updates:
        updates.setdefault("priority", "medium" if updates.pop("starred") else "normal")

    # Handle status shortcuts
    status = updates.pop("status", None)
    try:
        if status == "done":
            complete_task_by_id(task_id)
        elif status == "deleted":
            delete_task(task_id)
        result = update_task_by_id(task_id, updates) if updates else {"task": {}, "old": {}}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return result.get("task") or {}


@app.delete("/v1/tasks/{task_id}", status_code=204)
def delete_task_endpoint(task_id: str, agent: dict = Depends(_require_agent)):
    """Soft-delete a task by ID."""
    from skills.tasks.local_tasks import delete_task
    try:
        delete_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Agent action notification helper ────────────────────────────────────────


def _notify_agent_action(agent: dict, text: str) -> None:
    """Send a silent Telegram notification when an agent performs an action."""
    try:
        sender, channel_id = _default_recipient()
        write_outbox(
            channel_id=channel_id,
            sender=sender,
            text=text,
            source="telegram",
            silent=True,
        )
    except Exception:
        pass  # best-effort — don't fail the action


# ── Job scheduler endpoints ──────────────────────────────────────────────────

class CreateJobRequest(BaseModel):
    name: str                              # e.g. "fetch-travel-emails"
    schedule: str                          # cron: "*/10 * * * *" or interval: "15m"
    payload: Optional[dict] = None         # JSON passed to executor on fire


class JobResponse(BaseModel):
    id: str
    name: str
    schedule: str
    payload: Optional[dict] = None
    enabled: bool
    next_fire_at: Optional[str] = None
    last_fired_at: Optional[str] = None


class UpdateJobRequest(BaseModel):
    schedule: Optional[str] = None
    payload: Optional[dict] = None
    enabled: Optional[bool] = None


@app.post("/v1/jobs", response_model=JobResponse, status_code=201)
def create_job(body: CreateJobRequest, agent: dict = Depends(_require_agent)):
    """Create or update a scheduled job. Upserts by (agent_id, name)."""
    payload_str = json.dumps(body.payload or {})
    try:
        row = create_agent_job(agent["id"], body.name, body.schedule, payload_str)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JobResponse(
        id=row["id"],
        name=row["name"],
        schedule=row["schedule"],
        payload=json.loads(row.get("payload") or "{}") or None,
        enabled=bool(row["enabled"]),
        next_fire_at=row.get("next_fire_at"),
        last_fired_at=row.get("last_fired_at"),
    )


@app.get("/v1/jobs", response_model=list[JobResponse])
def list_jobs(agent: dict = Depends(_require_agent)):
    """List all jobs for the authenticated agent."""
    rows = list_agent_jobs(agent["id"])
    return [
        JobResponse(
            id=r["id"],
            name=r["name"],
            schedule=r["schedule"],
            payload=json.loads(r.get("payload") or "{}") or None,
            enabled=bool(r["enabled"]),
            next_fire_at=r.get("next_fire_at"),
            last_fired_at=r.get("last_fired_at"),
        )
        for r in rows
    ]


@app.patch("/v1/jobs/{job_id}", response_model=JobResponse)
def patch_job(job_id: str, body: UpdateJobRequest, agent: dict = Depends(_require_agent)):
    """Update schedule, payload, or enabled state of a job."""
    fields = {}
    if body.schedule is not None:
        fields["schedule"] = body.schedule
    if body.payload is not None:
        fields["payload"] = json.dumps(body.payload)
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    row = update_agent_job(agent["id"], job_id, **fields)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(
        id=row["id"],
        name=row["name"],
        schedule=row["schedule"],
        payload=json.loads(row.get("payload") or "{}") or None,
        enabled=bool(row["enabled"]),
        next_fire_at=row.get("next_fire_at"),
        last_fired_at=row.get("last_fired_at"),
    )


@app.delete("/v1/jobs/{job_id}", status_code=204)
def remove_job(job_id: str, agent: dict = Depends(_require_agent)):
    """Delete a scheduled job."""
    deleted = delete_agent_job(agent["id"], job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")


# ── Action delete (unified resource cleanup) ────────────────────────────────


@app.delete("/v1/actions/{action_id}", status_code=204)
def delete_action(action_id: str, agent: dict = Depends(_require_agent)):
    """Delete the resource(s) created by a previous action, identified by action_id.

    Dispatches to the appropriate delete function based on resource_type.
    Best-effort: already-deleted resources are silently skipped.
    """
    action = get_agent_action(agent["id"], action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")

    resource_ids = json.loads(action["resource_ids"])
    resource_type = action["resource_type"]

    if resource_type == "calendar_event":
        from skills.calendar.gog import delete_event
        cal_id = action.get("calendar_id") or ""
        for eid in resource_ids:
            try:
                delete_event(eid, calendar_id=cal_id)
            except Exception:
                pass  # event may already be deleted
    elif resource_type == "task":
        from skills.tasks.local_tasks import delete_task
        for tid in resource_ids:
            try:
                delete_task(tid)
            except ValueError:
                pass  # task may already be deleted

    delete_agent_action(agent["id"], action_id)

    _notify_agent_action(
        agent,
        f"🗑 *{agent['display_name']}* deleted {len(resource_ids)} {resource_type}(s). `#{action_id}`",
    )


# ── LLM endpoint ─────────────────────────────────────────────────────────────

_CLAUDE_MODEL_MAP = {"low": "haiku", "medium": "sonnet", "high": "opus"}


# Vision limits — mathe-trainer sends a question crop + at most a few option
# crops; anything bigger is a mistake on the caller's side, not a use case.
_LLM_MAX_IMAGES = 4
_LLM_MAX_IMAGE_BYTES = 2 * 1024 * 1024
_LLM_MAX_TOTAL_IMAGE_BYTES = 8 * 1024 * 1024
_LLM_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg"}


class LlmImage(BaseModel):
    data: str                    # base64 PNG/JPEG, no data: prefix
    media_type: Literal["image/png", "image/jpeg"] = "image/png"
    label: str = ""              # e.g. "the figure", "option (C)" — echoed into the prompt


class LlmRequest(BaseModel):
    prompt: str
    response_format: Literal["json", "text"] = "json"
    complexity: Literal["low", "medium", "high"] = "low"
    images: list[LlmImage] = []  # optional; ≤ 4, each ≤ 2 MB decoded, ≤ 8 MB total


class LlmResponse(BaseModel):
    text: str
    model: str
    provider: str       # "claude" | "gemini"
    duration_ms: int
    saw_images: int = 0  # how many of the request's images the provider actually received


def _decode_llm_images(images: list[LlmImage]) -> list[bytes]:
    """Validate the request's images. 413 on count/size, 422 on bad base64."""
    if len(images) > _LLM_MAX_IMAGES:
        raise HTTPException(status_code=413, detail={
            "error": "too_many_images", "max": _LLM_MAX_IMAGES, "got": len(images)})
    raw: list[bytes] = []
    for i, img in enumerate(images, 1):
        try:
            blob = base64.b64decode(img.data, validate=True)
        except Exception:
            raise HTTPException(status_code=422, detail={
                "error": "bad_image", "index": i, "reason": "not valid base64"})
        if not blob:
            raise HTTPException(status_code=422, detail={
                "error": "bad_image", "index": i, "reason": "empty"})
        if len(blob) > _LLM_MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail={
                "error": "image_too_large", "index": i,
                "max_bytes": _LLM_MAX_IMAGE_BYTES, "got": len(blob)})
        raw.append(blob)
    total = sum(len(b) for b in raw)
    if total > _LLM_MAX_TOTAL_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail={
            "error": "images_too_large", "max_bytes": _LLM_MAX_TOTAL_IMAGE_BYTES, "got": total})
    return raw


def _image_line(i: int, img: LlmImage, path: str) -> str:
    label = img.label.strip()
    return f"Image {i} ({label}): {path}" if label else f"Image {i}: {path}"


def _parse_claude_stream(stdout: str, image_paths: set[str]) -> tuple[str, int]:
    """Walk `--output-format stream-json` output. Returns (result_text, images_read).

    An image counts as read only when its Read tool_use got a non-error
    tool_result carrying an image block — a permission denial or a missing
    file leaves the model answering blind, and that must not count.
    """
    read_paths: dict[str, str] = {}   # tool_use_id → file_path
    seen: set[str] = set()
    result_text = ""
    result_error = ""
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        kind = ev.get("type")
        msg = ev.get("message") or {}
        content = msg.get("content")
        if kind == "assistant" and isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_use" and b.get("name") == "Read":
                    path = str((b.get("input") or {}).get("file_path", ""))
                    if path in image_paths:
                        read_paths[b.get("id", "")] = path
        elif kind == "user" and isinstance(content, list):
            for b in content:
                if b.get("type") != "tool_result" or b.get("is_error"):
                    continue
                path = read_paths.get(b.get("tool_use_id", ""))
                blocks = b.get("content")
                got_image = isinstance(blocks, list) and any(
                    isinstance(c, dict) and c.get("type") == "image" for c in blocks)
                if path and got_image:
                    seen.add(path)
        elif kind == "result":
            result_text = str(ev.get("result") or "")
            if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
                result_error = ev.get("subtype") or "error"
    if result_error:
        raise RuntimeError(f"claude stream ended with {result_error}: {result_text[:200]}")
    return result_text.strip(), len(seen)


def _call_claude_cli(prompt: str, model_alias: str, timeout: int,
                     images: "list[LlmImage] | None" = None,
                     image_bytes: "list[bytes] | None" = None) -> tuple[str, int]:
    """Run Claude CLI in print mode. Returns (response text, images seen).

    With images: each one is written to a private temp dir, the prompt gets one
    line per image naming the file, and the CLI runs with *only* the Read tool,
    allowed *only* inside that dir, so `claude -p` can look at them and nothing
    else. Raises RuntimeError on failure — including when images were sent but
    the model read none of them, so the caller falls through to a provider that
    takes images natively rather than returning a text-only guess.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude not on PATH")

    if not images:
        result = subprocess.run(
            [claude_bin, "-p", prompt,
             "--model", model_alias,
             "--output-format", "json",
             "--no-session-persistence"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:300]}")
        try:
            data = json.loads(result.stdout)
            return data.get("result", result.stdout).strip(), 0
        except (json.JSONDecodeError, TypeError):
            return result.stdout.strip(), 0

    tmp = _tempfile.mkdtemp(prefix="aaka-llm-")   # 0700
    try:
        paths: list[str] = []
        for i, (img, blob) in enumerate(zip(images, image_bytes or []), 1):
            path = os.path.join(tmp, f"{i}.{_LLM_IMAGE_EXT[img.media_type]}")
            with open(path, "wb") as fh:
                fh.write(blob)
            paths.append(path)
        listing = "\n".join(_image_line(i, img, p) for i, (img, p) in enumerate(zip(images, paths), 1))
        full_prompt = (
            f"{listing}\n"
            "Read every image above with the Read tool before answering.\n\n"
            f"{prompt}"
        )
        # Permission rule paths: `//` = absolute (a single `/` is project-relative).
        result = subprocess.run(
            [claude_bin, "-p", full_prompt,
             "--model", model_alias,
             "--output-format", "stream-json", "--verbose",
             "--no-session-persistence",
             "--tools", "Read",
             "--allowedTools", f"Read(/{tmp}/**)"],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, cwd=tmp,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:300]}")
        text, seen = _parse_claude_stream(result.stdout, set(paths))
        if seen == 0:
            raise RuntimeError(
                f"claude cli read none of the {len(images)} image(s) — "
                "vision via Read tool unavailable in this print-mode run")
        return text, seen
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _call_gemini_fallback(prompt: str, timeout: int,
                          images: "list[LlmImage] | None" = None) -> tuple[str, str]:
    """Call Gemini API. Returns (text, model_name). Raises on failure."""
    from gateway.adapter import GatewayAdapter
    adapter = GatewayAdapter()
    # _gemini_only=True: skip the Claude-CLI path the agent_api just tried.
    text = adapter._call_llm_fallback(
        prompt, timeout, _gemini_only=True,
        images=[img.model_dump() for img in images] if images else None)
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    return text, model


def _log_llm_endpoint(model: str, provider: str, status: str,
                      duration_ms: int, agent_id: str, error: str = "",
                      images: int = 0, saw_images: int = 0) -> None:
    config_dir = os.environ.get("AAKA_CONFIG_DIR", "/config")
    log_path = Path(config_dir) / "logs" / "llm-usage.jsonl"
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "provider": provider,
        "status": status,
        "duration_ms": duration_ms,
        "agent_id": agent_id,
        "source": "agent_api",
    }
    if images:
        entry["images"] = images
        entry["saw_images"] = saw_images
    if error:
        entry["error"] = error
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


@app.post("/v1/llm", response_model=LlmResponse)
def call_llm_endpoint(body: LlmRequest, agent: dict = Depends(_require_agent)):
    """Call an LLM via Claude CLI (subscription, zero cost) with Gemini fallback.

    Uses the local Claude subscription on Mac. Falls back to Gemini API if
    Claude CLI is unavailable or errors.
    """
    model_alias = _CLAUDE_MODEL_MAP[body.complexity]

    # Build effective prompt with format instructions
    if body.response_format == "json":
        effective_prompt = (
            "Respond with valid JSON only. No markdown fences, no commentary.\n\n"
            + body.prompt
        )
    else:
        effective_prompt = body.prompt

    images = body.images or None
    image_bytes = _decode_llm_images(body.images) if images else []
    n_images = len(image_bytes)

    t0 = time.monotonic()
    provider = "claude"
    model_used = model_alias
    saw_images = 0

    try:
        text, saw_images = _call_claude_cli(
            effective_prompt, model_alias, timeout=180 if images else 120,
            images=images, image_bytes=image_bytes)
    except (RuntimeError, subprocess.TimeoutExpired):
        # Fallback to Gemini — which takes images inline, so a Claude-CLI run
        # that could not read them still ends in a vision-backed answer.
        provider = "gemini"
        try:
            text, model_used = _call_gemini_fallback(effective_prompt, timeout=60, images=images)
            saw_images = n_images
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _log_llm_endpoint(model_used, provider, "error", duration_ms, agent["id"],
                              str(exc), images=n_images)
            from gateway.llm_providers import VisionUnsupported
            if isinstance(exc, VisionUnsupported):
                raise HTTPException(status_code=422, detail={
                    "error": "vision_unsupported", "provider": exc.provider})
            raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")

    duration_ms = int((time.monotonic() - t0) * 1000)
    _log_llm_endpoint(model_used, provider, "ok", duration_ms, agent["id"],
                      images=n_images, saw_images=saw_images)

    return LlmResponse(
        text=text,
        model=model_used,
        provider=provider,
        duration_ms=duration_ms,
        saw_images=saw_images,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Tag management endpoints ──────────────────────────────────────────────────

class RegisterTagRequest(BaseModel):
    tag: str                        # required: short name (e.g. "jp")
    alias: Optional[str] = None     # canonical name (e.g. "2607-japan-china")
    route: Optional[str] = None     # vault folder path (e.g. "travel/japan26")
    owner: Optional[str] = None     # member id for vault root (e.g. "family", "ari")


class RegisterTagResponse(BaseModel):
    status: str                     # "registered" | "already_set"
    tag: str
    alias: Optional[str] = None
    route: Optional[str] = None
    owner: Optional[str] = None


class RetireTagResponse(BaseModel):
    status: str                     # "retired" | "not_found"
    tag: str
    removed_alias: Optional[str] = None
    removed_route: Optional[str] = None
    removed_owner: Optional[str] = None


@app.post("/v1/tags", response_model=RegisterTagResponse, status_code=200)
def register_tag(body: RegisterTagRequest, agent: dict = Depends(_require_agent)):
    """Register a tag alias and/or route in references.yaml.

    Agents call this when creating a new project or trip so users can immediately
    take notes using the short tag name (e.g. n jp ...) without specifying the member.

    Example:
        POST /v1/tags {"tag": "jp", "alias": "2607-japan-china",
                       "route": "travel/japan26", "owner": "family"}
    """
    from tools.inbox_router import write_tag_to_references, load_references, resolve_topic_alias, resolve_route_keyword

    # Validate owner member exists (if provided)
    if body.owner:
        member = aaka_config.member_by_name(body.owner)
        if not member:
            raise HTTPException(status_code=400, detail=f"Unknown member: {body.owner}")

    written = write_tag_to_references(
        alias=body.tag if body.alias else None,
        alias_target=body.alias,
        route_key=body.alias or body.tag,
        route_path=body.route,
        route_owner=body.owner,
    )

    return RegisterTagResponse(
        status="registered" if written else "already_set",
        tag=body.tag,
        alias=body.alias,
        route=body.route,
        owner=body.owner,
    )


@app.delete("/v1/tags/{tag}", response_model=RetireTagResponse, status_code=200)
def retire_tag_endpoint(tag: str, agent: dict = Depends(_require_agent)):
    """Retire a tag — remove its alias and route from references.yaml.

    Frees the tag name for reuse on a new project or trip.
    The vault folder and _context.md are NOT deleted (they remain as archive).

    Example:
        DELETE /v1/tags/jp
    """
    from tools.inbox_router import retire_tag

    result = retire_tag(tag)
    if not result["found"]:
        raise HTTPException(status_code=404, detail=f"Tag '{tag}' not found in references")

    return RetireTagResponse(
        status="retired",
        tag=tag,
        removed_alias=result["removed_alias"],
        removed_route=result["removed_route"],
        removed_owner=result["removed_owner"],
    )


# ── PDF endpoints ─────────────────────────────────────────────────────────────
# File transfer uses base64 JSON — consistent with the existing JSON-only API,
# acceptable for localhost and files up to 50 MB.

class PdfCompressRequest(BaseModel):
    file_b64: str
    filename: str = "input.pdf"
    quality: int = 80
    dpi: int = 0   # 0 = no downsampling; 150 = screen; 200 = print


class PdfCompressResponse(BaseModel):
    file_b64: str
    filename: str
    original_kb: int
    compressed_kb: int


class PdfExtractRequest(BaseModel):
    file_b64: str
    filename: str = "input.pdf"


class PdfExtractResponse(BaseModel):
    file_b64: str          # unchanged PDF copy
    text: str              # per-page extracted text
    pages: int
    metadata: dict


class PdfSplitRequest(BaseModel):
    file_b64: str
    filename: str = "input.pdf"
    spec: str              # e.g. "1,5,9" / "3-5" / "2s"


class PdfFilePart(BaseModel):
    file_b64: str
    filename: str


class PdfSplitResponse(BaseModel):
    files: list[PdfFilePart]
    count: int


class PdfMergeRequest(BaseModel):
    files: list[PdfFilePart]


class PdfMergeResponse(BaseModel):
    file_b64: str
    filename: str
    pages: int


class PdfDeleteRequest(BaseModel):
    file_b64: str
    filename: str = "input.pdf"
    spec: str


class PdfDeleteResponse(BaseModel):
    file_b64: str
    filename: str
    removed: int
    remaining: int


class PdfOcrRequest(BaseModel):
    file_b64: str
    filename: str = "input.pdf"
    language: str = "eng"


class PdfOcrResponse(BaseModel):
    text: str
    pages: int
    ocr_pages: int
    chars: int


class PdfDeleteBlankRequest(BaseModel):
    file_b64: str
    filename: str = "input.pdf"
    return_blanks: bool = True   # False == dont-return


class PdfDeleteBlankResponse(BaseModel):
    file_b64: str
    filename: str
    blanks_file_b64: "str | None" = None
    blanks_filename: "str | None" = None
    removed: int
    remaining: int
    blank_pages: list[int]


def _decode_pdf(file_b64: str, filename: str, tmpdir: str) -> str:
    """Decode base64 PDF to a temp file and return its path."""
    try:
        data = base64.b64decode(file_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 in file_b64")
    dest = os.path.join(tmpdir, filename)
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def _encode_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _pdf_tool():
    try:
        from tools import pdf_tool as pt
        return pt
    except ImportError:
        raise HTTPException(status_code=503,
                            detail="pymupdf not installed — PDF tool unavailable")


@app.post("/v1/pdf/compress", response_model=PdfCompressResponse)
def pdf_compress(body: PdfCompressRequest, agent: dict = Depends(_require_agent)):
    """Compress a PDF. Returns the compressed file as base64."""
    pt = _pdf_tool()
    with _tempfile.TemporaryDirectory() as tmpdir:
        src = _decode_pdf(body.file_b64, body.filename, tmpdir)
        out = os.path.join(tmpdir, "compressed_" + body.filename)
        try:
            r = pt.compress(src, out, quality=body.quality, dpi=body.dpi)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return PdfCompressResponse(
            file_b64=_encode_file(r["output"]),
            filename="compressed_" + body.filename,
            original_kb=r["original_kb"],
            compressed_kb=r["compressed_kb"],
        )


@app.post("/v1/pdf/extract", response_model=PdfExtractResponse)
def pdf_extract(body: PdfExtractRequest, agent: dict = Depends(_require_agent)):
    """Extract per-page text and metadata from a PDF."""
    pt = _pdf_tool()
    with _tempfile.TemporaryDirectory() as tmpdir:
        src = _decode_pdf(body.file_b64, body.filename, tmpdir)
        try:
            r = pt.extract(src, tmpdir)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        text = Path(r["text_file"]).read_text(encoding="utf-8")
        return PdfExtractResponse(
            file_b64=_encode_file(r["output_pdf"]),
            text=text,
            pages=r["pages"],
            metadata=r["metadata"],
        )


@app.post("/v1/pdf/split", response_model=PdfSplitResponse)
def pdf_split(body: PdfSplitRequest, agent: dict = Depends(_require_agent)):
    """Split a PDF by page spec. Returns one base64 file per chunk."""
    pt = _pdf_tool()
    with _tempfile.TemporaryDirectory() as tmpdir:
        src = _decode_pdf(body.file_b64, body.filename, tmpdir)
        out_dir = os.path.join(tmpdir, "split")
        os.makedirs(out_dir, exist_ok=True)
        try:
            r = pt.split(src, body.spec, out_dir)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        parts = [
            PdfFilePart(
                file_b64=_encode_file(p),
                filename=Path(p).name,
            )
            for p in r["outputs"]
        ]
        return PdfSplitResponse(files=parts, count=r["count"])


@app.post("/v1/pdf/merge", response_model=PdfMergeResponse)
def pdf_merge(body: PdfMergeRequest, agent: dict = Depends(_require_agent)):
    """Merge multiple PDFs/images into one PDF."""
    pt = _pdf_tool()
    if not body.files:
        raise HTTPException(status_code=400, detail="No files provided for merge")
    with _tempfile.TemporaryDirectory() as tmpdir:
        paths = [_decode_pdf(f.file_b64, f.filename, tmpdir) for f in body.files]
        out = os.path.join(tmpdir, "merged.pdf")
        try:
            r = pt.merge(paths, out)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return PdfMergeResponse(
            file_b64=_encode_file(r["output"]),
            filename="merged.pdf",
            pages=r["pages"],
        )


@app.post("/v1/pdf/delete", response_model=PdfDeleteResponse)
def pdf_delete(body: PdfDeleteRequest, agent: dict = Depends(_require_agent)):
    """Remove pages from a PDF by page spec."""
    pt = _pdf_tool()
    with _tempfile.TemporaryDirectory() as tmpdir:
        src = _decode_pdf(body.file_b64, body.filename, tmpdir)
        stem = Path(body.filename).stem
        out = os.path.join(tmpdir, f"{stem}_trimmed.pdf")
        try:
            r = pt.delete_pages(src, body.spec, out)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return PdfDeleteResponse(
            file_b64=_encode_file(r["output"]),
            filename=f"{stem}_trimmed.pdf",
            removed=r["removed"],
            remaining=r["remaining"],
        )


@app.post("/v1/pdf/ocr", response_model=PdfOcrResponse)
def pdf_ocr(body: PdfOcrRequest, agent: dict = Depends(_require_agent)):
    """OCR a PDF with Tesseract; returns the extracted text."""
    pt = _pdf_tool()
    with _tempfile.TemporaryDirectory() as tmpdir:
        src = _decode_pdf(body.file_b64, body.filename, tmpdir)
        out = os.path.join(tmpdir, "ocr.txt")
        try:
            r = pt.ocr(src, out, language=body.language)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=f"OCR unavailable: {e}")
        text = Path(r["text_file"]).read_text(encoding="utf-8")
        return PdfOcrResponse(
            text=text,
            pages=r["pages"],
            ocr_pages=r["ocr_pages"],
            chars=r["chars"],
        )


@app.post("/v1/pdf/delete-blank-pages", response_model=PdfDeleteBlankResponse)
def pdf_delete_blank_pages(body: PdfDeleteBlankRequest,
                           agent: dict = Depends(_require_agent)):
    """Auto-detect and remove blank pages.

    Returns the trimmed file and (unless return_blanks=false) a companion file
    of the removed pages so the caller can verify the detection was correct.
    """
    pt = _pdf_tool()
    with _tempfile.TemporaryDirectory() as tmpdir:
        src = _decode_pdf(body.file_b64, body.filename, tmpdir)
        stem = Path(body.filename).stem
        out = os.path.join(tmpdir, f"{stem}_trimmed.pdf")
        blanks_out = os.path.join(tmpdir, f"{stem}_blanks.pdf")
        try:
            r = pt.delete_blank_pages(
                src, out, blanks_out, write_blanks=body.return_blanks,
            )
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        blanks_b64 = None
        blanks_name = None
        if r["blanks_output"]:
            blanks_b64 = _encode_file(r["blanks_output"])
            blanks_name = f"{stem}_blanks.pdf"
        return PdfDeleteBlankResponse(
            file_b64=_encode_file(r["output"]),
            filename=f"{stem}_trimmed.pdf",
            blanks_file_b64=blanks_b64,
            blanks_filename=blanks_name,
            removed=r["removed"],
            remaining=r["remaining"],
            blank_pages=r["blank_pages"],
        )


# ── Photo sending ─────────────────────────────────────────────────────────────

@app.post("/v1/photos", response_model=SendPhotoResponse, status_code=201)
def send_photo(body: SendPhotoRequest, agent: dict = Depends(_require_agent)):
    """Send a photo to the user via the egress gateway (audited, rate-limited)."""
    import base64
    from gateway.egress import send as _egress_send, OutboundMessage, MessageKind

    sender = body.sender
    channel_id = body.channel_id
    if not sender:
        sender, channel_id = _default_recipient()
    chat_id = channel_id or sender

    try:
        photo_bytes = base64.b64decode(body.photo_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 photo_b64: {exc}")

    try:
        _egress_send(OutboundMessage(
            kind=MessageKind.PHOTO,
            recipient=str(chat_id),
            channel="telegram",
            photo_bytes=photo_bytes,
            photo_caption=body.caption or "",
            source=f"agent_api:{agent['id']}",
            silent=bool(body.silent),
        ))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Photo send failed: {exc}")

    return SendPhotoResponse(ok=True, message_id=None)


# ── Approval gate ─────────────────────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    intent: str = "linkedin_post"         # skill to run on approval
    payload: dict                          # skill-specific payload (text, image_path, etc.)
    schedule_at: Optional[str] = None     # ISO 8601 UTC; None = post immediately on approval
    sender: Optional[str] = None          # override recipient; defaults to first admin
    channel_id: Optional[str] = None


class ApprovalResponse(BaseModel):
    approval_id: str
    item_id: str
    status: str


class ApprovalStatusResponse(BaseModel):
    approval_id: str
    item_id: str
    status: str                            # awaiting_confirm | scheduled | confirmed | done | cancelled | error
    result: Optional[dict] = None


@app.post("/v1/approvals", response_model=ApprovalResponse, status_code=201)
def create_approval(body: ApprovalRequest, agent: dict = Depends(_require_agent)):
    """Queue an action that requires human approval before execution.

    Sends a Telegram preview to the default recipient with Approve/Cancel buttons.
    Poll GET /v1/approvals/{approval_id} for status.
    """
    from aaka_queue.queue import write_approval_item, set_pending_confirm

    approval_id = str(uuid.uuid4())
    sender, channel_id = _default_recipient()
    if body.sender:
        sender = body.sender
    if body.channel_id:
        channel_id = body.channel_id

    payload = dict(body.payload)
    payload["approval_id"] = approval_id
    payload["schedule_at"] = body.schedule_at
    payload["source"] = "telegram"

    # Warn if a linkedin_post is submitted without an image — draft will lack media
    if body.intent == "linkedin_post" and not payload.get("image_path"):
        import logging as _log
        _log.getLogger(__name__).warning(
            "approval linkedin_post submitted without image_path — draft will have no media"
        )

    item_id = write_approval_item(
        intent=body.intent,
        raw_message=f"[agent:{agent['id']}] {body.intent}",
        sender=sender,
        channel_id=channel_id,
        source="telegram",
        payload=payload,
        approval_id=approval_id,
        schedule_at=body.schedule_at,
    )
    set_pending_confirm(sender, item_id)

    # Build preview text — keep total message ≤ 3800 chars so buttons fit in one Telegram message
    _TG_PREVIEW_MAX = 3800
    if body.intent == "linkedin_post":
        post_text = payload.get("text", "")
        import re as _re
        tg_text = _re.sub(r'\*\*(.+?)\*\*', r'*\1*', post_text)
        sched_str = f"\n⏰ Scheduled: {body.schedule_at[:16].replace('T', ' ')} UTC" if body.schedule_at else ""
        header = f"📤 *LinkedIn post (via {agent['display_name']}):*\n\n"
        footer = f"{sched_str}\n\nApprove?\n`#{item_id[:8]}`"
        max_body = _TG_PREVIEW_MAX - len(header) - len(footer)
        if len(tg_text) > max_body:
            tg_text = tg_text[:max_body - 1] + "…"
        preview = f"{header}{tg_text}{footer}"
    else:
        preview = f"🔔 *Action requires approval ({body.intent})*\n\nApprove?\n`#{item_id[:8]}`"

    _markup = {"inline_keyboard": [[
        {"text": "Post ✅", "callback_data": "yes"},
        {"text": "Cancel ❌", "callback_data": "cancel"},
    ]]}
    write_outbox(
        channel_id=channel_id, sender=sender,
        text=preview,
        source="telegram",
        reply_markup=_markup,
    )

    return ApprovalResponse(approval_id=approval_id, item_id=item_id, status="awaiting_confirm")


@app.get("/v1/approvals/{approval_id}", response_model=ApprovalStatusResponse)
def get_approval_status(approval_id: str, agent: dict = Depends(_require_agent)):
    """Poll the status of a queued approval request."""
    from aaka_queue.queue import get_item_by_approval_id
    item = get_item_by_approval_id(approval_id)
    if not item:
        raise HTTPException(status_code=404, detail="Approval not found")

    result = None
    if item.get("result"):
        try:
            result = json.loads(item["result"])
        except Exception:
            pass

    return ApprovalStatusResponse(
        approval_id=approval_id,
        item_id=item["id"],
        status=item["status"],
        result=result,
    )


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("AGENT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_API_PORT", "18790"))
    uvicorn.run("gateway.agent_api:app", host=host, port=port, reload=False)
