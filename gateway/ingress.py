"""
gateway/ingress.py — Unified inbound message entry point.

ALL inbound messages (Telegram, WhatsApp, Gmail, Agent API) must call
ingress.receive() before routing. This gives us:

  • A unified audit log    ($LOGS_DIR/ingress.jsonl)
  • Sender block check     (blocked_senders.json checked once, for all channels)
  • Metadata normalization (Format A / Format B / Format C → ParsedMessage)
  • Correlation ID         (shared with egress.jsonl for full trace)

Usage:
    from gateway.ingress import receive, InboundMessage, Channel

    parsed = receive(InboundMessage(
        raw_text="hello",
        sender_id="123456789",
        channel=Channel.TELEGRAM,
        message_id="42",
        source="telegram_poller",
    ))
    if parsed is None:
        return  # blocked sender or format error
    # parsed.text, parsed.sender_id, parsed.channel_id, etc.

The module is import-safe in both VPS (sensor) and Mac (executor) contexts.
Heavy dependencies (aaka_config, queue) are imported lazily.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ── Types ──────────────────────────────────────────────────────────────────────


class Channel(str, Enum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    AGENT = "agent"   # injected by agent API


@dataclass
class InboundMessage:
    """Raw inbound message before normalization."""
    raw_text: str
    sender_id: str
    channel: Channel | str
    source: str               # caller name, e.g. "telegram_poller"

    message_id: Optional[str] = None
    channel_id: Optional[str] = None   # chat_id (TG), group JID (WA), etc.
    media_path: Optional[str] = None
    mime_type: Optional[str] = None
    timestamp: Optional[str] = None    # ISO 8601 UTC


@dataclass
class ParsedMessage:
    """Normalized inbound message ready for intent routing."""
    text: str
    sender_id: str
    channel: str              # "telegram" | "whatsapp" | "email" | "agent"
    channel_id: str           # canonical chat/group ID
    message_id: Optional[str]
    source: str

    media_path: Optional[str] = None
    mime_type: Optional[str] = None
    # Filename with OpenClaw's ---uuid suffix stripped, e.g. "invoice.pdf"
    original_filename: Optional[str] = None

    # True when the WhatsApp sender is the account's own phone (self-DM)
    is_self_dm: bool = False

    # Set by ingress.receive() — used to correlate with egress log
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # Original raw_text (before normalization) — for audit
    raw_text: str = ""


# ── Config helpers ─────────────────────────────────────────────────────────────

def _config_dir() -> Path:
    return Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))


def _logs_dir() -> Path:
    d = _config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Blocked senders ────────────────────────────────────────────────────────────

_blocked_cache: set = set()
_blocked_mtime: float = 0.0
_BLOCKED_TTL = 10.0  # re-read file at most every 10 seconds


def _load_blocked() -> set:
    global _blocked_cache, _blocked_mtime
    now = time.monotonic()
    if now - _blocked_mtime < _BLOCKED_TTL:
        return _blocked_cache
    blocked_path = _config_dir() / "data" / "blocked_senders.json"
    if blocked_path.exists():
        try:
            data = json.loads(blocked_path.read_text())
            _blocked_cache = set(data if isinstance(data, list) else data.keys())
        except Exception:
            pass
    _blocked_mtime = now
    return _blocked_cache


def is_blocked(sender_id: str) -> bool:
    return str(sender_id) in _load_blocked()


# ── Audit log ──────────────────────────────────────────────────────────────────

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _log(msg: InboundMessage, status: str, correlation_id: str = "",
         intent: str = "") -> None:
    """Append one JSON line to ingress.jsonl."""
    entry: dict = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sender": msg.sender_id,
        "channel": msg.channel.value if isinstance(msg.channel, Channel) else str(msg.channel),
        "channel_id": msg.channel_id or "",
        "source": msg.source,
        "content_hash": _content_hash(msg.raw_text),
        "message_id": msg.message_id or "",
        "status": status,
    }
    if correlation_id:
        entry["correlation_id"] = correlation_id
    if intent:
        entry["intent"] = intent
    try:
        log_path = _logs_dir() / "ingress.jsonl"
        with open(log_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # non-fatal


# ── Metadata normalization ─────────────────────────────────────────────────────
#
# Format A (telegram_poller):
#   Conversation info (untrusted metadata):
#   ```json
#   {"chat_id": "telegram:...", "message_id": "...", "sender_id": "...", "conversation_label": "id:..."}
#   ```
#   [media attached: /path/to/file (mime/type)]
#   <text>
#
# Format B (WhatsApp via OpenClaw cliBackend):
#   [WhatsApp +10000000000 +18m Fri 2026-04-24 18:24 UTC] (self): [openclaw] /cmd
#
# Format C (plain text — fallback):
#   <text>
#   (sender/channel come from InboundMessage.sender_id / .channel)

_FORMAT_A_RE = re.compile(
    r'Conversation info[^`]+```json\s*(\{[^}]+\})\s*```',
    re.DOTALL,
)
_FORMAT_A_MEDIA_RE = re.compile(
    r'\[media attached:\s*(.+?)\s+\(([^)]+)\)\s*(?:\|[^\]]+)?\]'
)
# Format B: OpenClaw WhatsApp cliBackend envelope
_FORMAT_B_WA_RE = re.compile(
    r'^\[(\w+)\s+'           # channel name (WhatsApp, Telegram, …)
    r'(\+?[\d@.\w]+)\s+'     # sender ID (phone or JID)
    r'.*?\]\s*'              # timing + date — ignored
    r'\(([^)]*)\):\s*'       # role: (self), (user), etc.
    r'(?:\[openclaw\]\s*)?'  # optional [openclaw] tag
    r'(.*)',                  # actual message text
    re.DOTALL,
)


def _strip_uuid_suffix(path: str) -> str:
    """Strip the ---<uuid> suffix that OpenClaw adds to uploaded filenames."""
    basename = os.path.basename(path)
    m = re.match(r'^(.+?)---[0-9a-f-]+(\.\w+)$', basename)
    return (m.group(1) + m.group(2)) if m else basename


def _clean_media_text(text: str) -> str:
    """Strip OpenClaw's image/PDF wrapper markup from message text.

    OpenClaw wraps media messages with patterns like:
      [Image]\\nUser text:\\n[Telegram P G ...] /drop photo\\nDescription:\\n...
      <file name="..." mime="...">...</file>
      /tmp/openclaw/...
    """
    text = re.sub(r'<file\b[^>]*>.*?</file>', '', text, flags=re.DOTALL)
    text = re.sub(r'/tmp/openclaw/[^\n]+', '', text)
    user_text_match = re.search(r'User text:\s*\n(.+)', text)
    if user_text_match:
        text = user_text_match.group(1).strip()
    else:
        desc_idx = text.find('\nDescription:')
        if desc_idx != -1:
            text = text[:desc_idx].strip()
    text = re.sub(r'^\[(?:Telegram|WhatsApp)\s+[^\]]+\]\s*', '', text)
    text = re.sub(r'^\[Image\]\s*', '', text)
    return text.strip()


def normalize(msg: InboundMessage) -> Optional[ParsedMessage]:
    """Normalise InboundMessage into a ParsedMessage.

    Returns None if the message cannot be parsed (e.g. no sender_id).
    """
    raw = msg.raw_text
    sender_id = msg.sender_id
    channel_id = msg.channel_id or ""
    message_id = msg.message_id
    channel = msg.channel.value if isinstance(msg.channel, Channel) else str(msg.channel)
    media_path = msg.media_path
    mime_type = msg.mime_type
    original_filename: Optional[str] = None
    is_self_dm = False
    text = raw

    # ── Format A (Telegram poller envelope) ──────────────────────────────────
    m_a = _FORMAT_A_RE.search(raw)
    if m_a:
        try:
            meta = json.loads(m_a.group(1))
            sender_id = str(meta.get("sender_id", sender_id))
            message_id = str(meta.get("message_id", message_id or ""))
            # channel + channel_id from "chat_id": "<channel>:<id>" (telegram:… /
            # whatsapp:…). Defaults to telegram for back-compat with older envelopes.
            chat_id = meta.get("chat_id", "")
            if chat_id.startswith("whatsapp:"):
                channel = "whatsapp"
                channel_id = chat_id[len("whatsapp:"):]
            elif chat_id.startswith("telegram:"):
                channel = "telegram"
                channel_id = chat_id[len("telegram:"):]
            else:
                channel = "telegram"
            # "conversation_label": "id:<chat_id>" overrides channel_id when present
            label = meta.get("conversation_label", "")
            if label.startswith("id:"):
                channel_id = label[3:]
        except Exception:
            pass
        # Strip the metadata envelope from the text
        text = raw[m_a.end():].strip()
        # Extract optional media header
        m_media = _FORMAT_A_MEDIA_RE.search(text)
        if m_media:
            media_path = m_media.group(1)
            mime_type = m_media.group(2)
            original_filename = _strip_uuid_suffix(media_path)
            text = (text[:m_media.start()] + text[m_media.end():]).strip()
        # Strip OpenClaw image/PDF wrapper markup from the remaining text
        text = _clean_media_text(text) if text else text

    # ── Format B (WhatsApp via OpenClaw cliBackend) ───────────────────────────
    elif _FORMAT_B_WA_RE.match(raw):
        m_b = _FORMAT_B_WA_RE.match(raw)
        channel = m_b.group(1).lower()       # "whatsapp"
        sender_id = m_b.group(2)
        role = m_b.group(3)                  # "self", "user", etc.
        is_self_dm = (role == "self")
        text = m_b.group(4).strip()
        # No channel_id in this format — falls through to DM fallback below

    # ── Format C (plain text) — no envelope, use what was provided ────────────
    # text = raw as-is, sender/channel from InboundMessage fields

    if not sender_id:
        return None  # Can't route without a sender

    if not channel_id:
        channel_id = sender_id  # DM: use sender as channel

    return ParsedMessage(
        text=text,
        sender_id=sender_id,
        channel=channel,
        channel_id=channel_id,
        message_id=message_id,
        source=msg.source,
        media_path=media_path,
        mime_type=mime_type,
        original_filename=original_filename,
        is_self_dm=is_self_dm,
        raw_text=raw,
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def receive(msg: InboundMessage, *, log_intent: str = "") -> Optional[ParsedMessage]:
    """Process an inbound message through the ingress gateway.

    - Logs to ingress.jsonl
    - Checks blocked_senders.json — returns None if blocked
    - Normalises metadata (Format A / B / C)
    - Returns ParsedMessage with correlation_id set

    Args:
        msg:        The raw inbound message.
        log_intent: If known at call time, record in the log entry.
                    Callers can also update the log after routing by calling
                    log_intent_resolved(correlation_id, intent).

    Returns:
        ParsedMessage on success, None if blocked or unparseable.
    """
    # Blocked sender check
    if is_blocked(str(msg.sender_id)):
        _log(msg, "blocked")
        return None

    parsed = normalize(msg)
    if parsed is None:
        _log(msg, "parse_error")
        return None

    _log(msg, "received", correlation_id=parsed.correlation_id, intent=log_intent)
    return parsed


def log_intent_resolved(correlation_id: str, intent: str) -> None:
    """Update the ingress log entry with the resolved intent.

    Call this after routing so the log shows what the message became.
    Appends a separate 'intent_resolved' entry (simpler than rewriting).
    """
    try:
        log_path = _logs_dir() / "ingress.jsonl"
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "correlation_id": correlation_id,
            "status": "intent_resolved",
            "intent": intent,
        }
        with open(log_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── Admin helpers ──────────────────────────────────────────────────────────────

def ingress_status(hours: int = 1) -> dict:
    """Read ingress.jsonl and return a summary dict for the last N hours."""
    log_path = _logs_dir() / "ingress.jsonl"
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    total = 0
    by_status: dict[str, int] = {}
    by_sender: dict[str, int] = {}
    by_channel: dict[str, int] = {}

    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("ts", "") < cutoff:
                continue
            if entry.get("status") == "intent_resolved":
                continue
            total += 1
            status = entry.get("status", "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            sender = entry.get("sender", "?")
            by_sender[sender] = by_sender.get(sender, 0) + 1
            ch = entry.get("channel", "?")
            by_channel[ch] = by_channel.get(ch, 0) + 1

    return {
        "total": total,
        "by_status": by_status,
        "by_sender": by_sender,
        "by_channel": by_channel,
    }


def trace(correlation_id: str) -> list[dict]:
    """Return all log entries (ingress + egress) for a given correlation_id.

    Reads both ingress.jsonl and egress.jsonl and returns entries sorted by ts.
    """
    entries: list[dict] = []
    for log_name in ("ingress.jsonl", "egress.jsonl"):
        log_path = _logs_dir() / log_name
        if not log_path.exists():
            continue
        for line in log_path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("correlation_id") == correlation_id:
                entry["_log"] = log_name
                entries.append(entry)
    entries.sort(key=lambda e: e.get("ts", ""))
    return entries
