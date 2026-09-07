"""
gateway/egress.py — Single outbound message chokepoint.

ALL messages leaving aaka (text, photo, document, reaction) must flow through
egress.send(). This gives us:

  • A unified audit log  ($LOGS_DIR/egress.jsonl)
  • A kill switch        ($AAKA_CONFIG_DIR/data/egress_enabled.json)
  • Per-recipient rate limiting (30 msg/hour default, in-memory with TTL)
  • Channel abstraction  (see gateway/channels/ for per-channel adapters)

Usage:
    from gateway.egress import send, OutboundMessage, MessageKind

    send(OutboundMessage(
        kind=MessageKind.TEXT,
        recipient="123456789",
        channel="telegram",
        text="Hello!",
        source="flush_outbox",
    ))

Adding a new channel:
    1. Create gateway/channels/<name>.py implementing send_text(), send_photo(),
       send_document(), send_reaction()
    2. Add one entry to _CHANNEL_DISPATCH below

The module is intentionally dependency-light so it runs on both VPS (sensor)
and Mac (executor) without extra packages.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

# Re-export types so callers can use:
#   from gateway.egress import OutboundMessage, MessageKind
from gateway.types import MessageKind, OutboundMessage  # noqa: F401


# ── Channel dispatch map ───────────────────────────────────────────────────────
# Maps channel name → module.  Each module must expose send_text(), send_photo(),
# send_document(), send_reaction().  Lazy imports keep startup cost low.

def _get_telegram():
    from gateway.channels import telegram
    return telegram

def _get_whatsapp():
    from gateway.channels import whatsapp
    return whatsapp

def _get_slack():
    from gateway.channels import slack
    return slack

def _get_signal():
    # Module is signal_cli.py, not signal.py — a gateway/channels/signal.py would
    # shadow the stdlib `signal` module for anything importing from that dir.
    from gateway.channels import signal_cli
    return signal_cli

_CHANNEL_DISPATCH = {
    "telegram": _get_telegram,
    "whatsapp": _get_whatsapp,
    "slack": _get_slack,
    "signal": _get_signal,
}

_KIND_METHOD = {
    MessageKind.TEXT:     "send_text",
    MessageKind.PHOTO:    "send_photo",
    MessageKind.DOCUMENT: "send_document",
    MessageKind.REACTION: "send_reaction",
}


# ── Config helpers ─────────────────────────────────────────────────────────────

def _config_dir() -> Path:
    return Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))


def _logs_dir() -> Path:
    d = _config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Kill switch ────────────────────────────────────────────────────────────────

_kill_switch_cache: bool = True
_kill_switch_mtime: float = 0.0
_KILL_SWITCH_TTL = 5.0


def _egress_enabled() -> bool:
    global _kill_switch_cache, _kill_switch_mtime
    now = time.monotonic()
    if now - _kill_switch_mtime < _KILL_SWITCH_TTL:
        return _kill_switch_cache
    flag_path = _config_dir() / "data" / "egress_enabled.json"
    if flag_path.exists():
        try:
            _kill_switch_cache = bool(json.loads(flag_path.read_text()).get("enabled", True))
        except Exception:
            _kill_switch_cache = True
    else:
        _kill_switch_cache = True
    _kill_switch_mtime = now
    return _kill_switch_cache


def set_egress_enabled(enabled: bool) -> None:
    """Programmatically set the kill switch. Persists to disk."""
    flag_path = _config_dir() / "data" / "egress_enabled.json"
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(json.dumps({"enabled": enabled}))
    global _kill_switch_cache, _kill_switch_mtime
    _kill_switch_cache = enabled
    _kill_switch_mtime = time.monotonic()


# ── Rate limiting ──────────────────────────────────────────────────────────────

_rate_buckets: "dict[str, list[float]]" = {}
RATE_LIMIT_PER_HOUR: int = int(os.environ.get("EGRESS_RATE_LIMIT", "30"))


def _check_rate_limit(recipient: str) -> bool:
    now = time.time()
    window_start = now - 3600.0
    bucket = [t for t in _rate_buckets.get(recipient, []) if t >= window_start]
    _rate_buckets[recipient] = bucket
    if len(bucket) >= RATE_LIMIT_PER_HOUR:
        return False
    bucket.append(now)
    _rate_buckets[recipient] = bucket
    return True


# ── Audit log ──────────────────────────────────────────────────────────────────

def _content_hash(msg: OutboundMessage) -> str:
    content = msg.text or msg.photo_caption or msg.file_caption or msg.emoji or ""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _log(msg: OutboundMessage, status: str, error: str = "") -> None:
    entry: dict = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": msg.kind,
        "recipient": msg.recipient,
        "channel": msg.channel,
        "source": msg.source,
        "content_hash": _content_hash(msg),
        "status": status,
    }
    if msg.correlation_id:
        entry["correlation_id"] = msg.correlation_id
    if error:
        entry["error"] = error
    try:
        with open(_logs_dir() / "egress.jsonl", "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── Main entry point ───────────────────────────────────────────────────────────

def send(msg: OutboundMessage) -> None:
    """Send an outbound message through the egress gateway.

    Checks kill switch → rate limit → dispatches to channel adapter → audits.
    """
    if not _egress_enabled():
        _log(msg, "killed", error="egress disabled by kill switch")
        return

    if not _check_rate_limit(msg.recipient):
        _log(msg, "rate_limited",
             error=f"exceeded {RATE_LIMIT_PER_HOUR} msg/hour for recipient {msg.recipient}")
        return

    channel_factory = _CHANNEL_DISPATCH.get(msg.channel)
    if channel_factory is None:
        err = f"Unknown channel: {msg.channel!r}"
        _log(msg, "error", error=err)
        raise RuntimeError(err)

    try:
        adapter = channel_factory()
        method_name = _KIND_METHOD.get(msg.kind)
        if method_name is None:
            raise RuntimeError(f"Unknown MessageKind: {msg.kind!r}")
        getattr(adapter, method_name)(msg)
        _log(msg, "sent")
    except Exception as exc:
        _log(msg, "error", error=str(exc))
        raise


# ── Convenience constructors ───────────────────────────────────────────────────

def text(recipient: str, channel: str, content: str, source: str, *,
         reply_to: str = None, silent: bool = False,
         markup: dict = None, correlation_id: str = None) -> OutboundMessage:
    return OutboundMessage(
        kind=MessageKind.TEXT, recipient=recipient, channel=channel,
        text=content, source=source, reply_to_message_id=reply_to,
        silent=silent, reply_markup=markup, correlation_id=correlation_id,
    )


def photo(recipient: str, channel: str, photo_bytes: bytes, source: str, *,
          caption: str = "", reply_to: str = None,
          correlation_id: str = None) -> OutboundMessage:
    return OutboundMessage(
        kind=MessageKind.PHOTO, recipient=recipient, channel=channel,
        photo_bytes=photo_bytes, photo_caption=caption, source=source,
        reply_to_message_id=reply_to, correlation_id=correlation_id,
    )


def document(recipient: str, channel: str, file_path: str, source: str, *,
             caption: str = "", reply_to: str = None,
             correlation_id: str = None) -> OutboundMessage:
    return OutboundMessage(
        kind=MessageKind.DOCUMENT, recipient=recipient, channel=channel,
        file_path=file_path, file_caption=caption, source=source,
        reply_to_message_id=reply_to, correlation_id=correlation_id,
    )


def reaction(recipient: str, channel: str, emoji: str, message_id: str,
             source: str, *, correlation_id: str = None) -> OutboundMessage:
    return OutboundMessage(
        kind=MessageKind.REACTION, recipient=recipient, channel=channel,
        emoji=emoji, reaction_message_id=message_id, source=source,
        correlation_id=correlation_id,
    )


# ── Admin helpers ──────────────────────────────────────────────────────────────

def egress_status(hours: int = 1) -> dict:
    """Read egress.jsonl and return a summary dict for the last N hours."""
    log_path = _logs_dir() / "egress.jsonl"
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = 0
    by_status: "dict[str, int]" = {}
    by_recipient: "dict[str, int]" = {}
    recent_errors: "list[dict]" = []

    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("ts", "") < cutoff:
                continue
            total += 1
            status = entry.get("status", "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            recip = entry.get("recipient", "?")
            by_recipient[recip] = by_recipient.get(recip, 0) + 1
            if status == "error":
                recent_errors.append(entry)

    return {
        "enabled": _egress_enabled(),
        "total": total,
        "by_status": by_status,
        "by_recipient": by_recipient,
        "recent_errors": recent_errors[-5:],
    }
