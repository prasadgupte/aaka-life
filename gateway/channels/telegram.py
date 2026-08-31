"""
gateway/channels/telegram.py — Telegram Bot API channel adapter.

Handles all four message kinds: text (with auto-split + Markdown fallback),
photo (multipart), document (multipart), reaction (setMessageReaction).

The bot token is loaded once per process with a 60-second TTL:
  env TELEGRAM_BOT_TOKEN → $AAKA_CONFIG_DIR/tokens/message_send.json

All public send_* functions accept an OutboundMessage from gateway.types.
"""
from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from gateway.types import MessageKind, OutboundMessage

# ── Token loading (cached, TTL 60 s) ──────────────────────────────────────────

_token_cache: str = ""
_token_mtime: float = 0.0
_TOKEN_TTL = 60.0

_bots_cache: "dict[str, str]" = {}
_bots_mtime: float = 0.0


def _load_bots() -> "dict[str, str]":
    """Load the multi-bot token map from tokens/telegram_bots.json (cached).

    Shape: {"<bot_id>": "<token>", ...}. Used for multi-bot deployments.
    """
    global _bots_cache, _bots_mtime
    now = time.monotonic()
    if _bots_cache and now - _bots_mtime < _TOKEN_TTL:
        return _bots_cache
    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
    path = config_dir / "tokens" / "telegram_bots.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                _bots_cache = {k: str(v) for k, v in data.items() if v}
                _bots_mtime = now
        except Exception:
            pass
    return _bots_cache


def bot_token(bot_id: "str | None" = None) -> str:
    """Return the Telegram bot token, optionally for a specific bot.

    Multi-bot (bot_id given): env TELEGRAM_BOT_TOKEN_<BOTID> → tokens/telegram_bots.json.
    Falls back to the default single-bot token if the bot_id isn't configured.
    Default: TELEGRAM_BOT_TOKEN env → tokens/message_send.json. TTL-cached.
    """
    global _token_cache, _token_mtime

    if bot_id:
        env_key = "TELEGRAM_BOT_TOKEN_" + bot_id.upper().replace("-", "_")
        env_tok = os.environ.get(env_key, "")
        if env_tok:
            return env_tok
        tok = _load_bots().get(bot_id, "")
        if tok:
            return tok
        # fall through to the default token (single-bot back-compat)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return token

    now = time.monotonic()
    if _token_cache and now - _token_mtime < _TOKEN_TTL:
        return _token_cache

    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
    creds = config_dir / "tokens" / "message_send.json"
    if creds.exists():
        try:
            _token_cache = json.loads(creds.read_text()).get("bot_token", "")
            _token_mtime = now
        except Exception:
            pass

    return _token_cache


# ── Low-level HTTP helper ──────────────────────────────────────────────────────

_TG_BASE = "https://api.telegram.org/bot"


def _post(method: str, payload: dict, timeout: int = 15,
          bot_id: "str | None" = None) -> dict:
    """POST to Telegram Bot API. Returns parsed JSON or raises RuntimeError."""
    token = bot_token(bot_id)
    if not token:
        raise RuntimeError("No Telegram bot token available")
    url = f"{_TG_BASE}{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram {method} HTTP {exc.code}: {exc.read()[:200]}") from exc


def _post_multipart(method: str, parts: list[bytes], boundary: str,
                    timeout: int = 60, bot_id: "str | None" = None) -> dict:
    """POST multipart/form-data to Telegram Bot API."""
    token = bot_token(bot_id)
    if not token:
        raise RuntimeError("No Telegram bot token available")
    url = f"{_TG_BASE}{token}/{method}"
    body = b"".join(parts)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
            if not resp.get("ok"):
                raise RuntimeError(f"{method} failed: {resp}")
            return resp
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} HTTP {exc.code}: {exc.read()[:200]}") from exc


def _field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()


# ── Text ──────────────────────────────────────────────────────────────────────

_MAX_LEN = 4000  # Telegram hard limit is 4096; leave headroom


def _split_text(text: str) -> list[str]:
    if len(text) <= _MAX_LEN:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= _MAX_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, _MAX_LEN)
        if split_at <= 0:
            split_at = _MAX_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def send_text(msg: OutboundMessage) -> None:
    text = msg.text
    markup = None

    # Support legacy __MARKUP__: sentinel from router_sensor path
    if "__MARKUP__:" in text:
        text, markup_json = text.rsplit("__MARKUP__:", 1)
        text = text.rstrip()
        try:
            markup = json.loads(markup_json)
        except Exception:
            pass
    if msg.reply_markup and not markup:
        markup = msg.reply_markup

    chunks = _split_text(text)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        params: dict = {"chat_id": msg.recipient, "text": chunk}
        if i == 0 and msg.reply_to_message_id:
            params["reply_to_message_id"] = int(msg.reply_to_message_id)
        if msg.silent:
            params["disable_notification"] = True
            params["disable_web_page_preview"] = True
        if markup and is_last:
            params["reply_markup"] = markup

        # First attempt: Markdown parse mode
        try:
            resp = _post("sendMessage", {**params, "parse_mode": "Markdown"}, bot_id=msg.bot_id)
            if resp.get("ok"):
                continue
        except RuntimeError as exc:
            if "400" not in str(exc):
                raise
        # Fallback: plain text (no parse_mode)
        _post("sendMessage", params, bot_id=msg.bot_id)


# ── Photo ─────────────────────────────────────────────────────────────────────

def send_photo(msg: OutboundMessage) -> None:
    boundary = "----AakaPhotoBoundary"
    parts: list[bytes] = [_field(boundary, "chat_id", str(msg.recipient))]
    if msg.photo_caption:
        parts.append(_field(boundary, "caption", msg.photo_caption[:1024]))
    if msg.reply_to_message_id:
        parts.append(_field(boundary, "reply_to_message_id", str(msg.reply_to_message_id)))
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="photo.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + (msg.photo_bytes or b"")
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    _post_multipart("sendPhoto", parts, boundary, timeout=30, bot_id=msg.bot_id)


# ── Document ──────────────────────────────────────────────────────────────────

def send_document(msg: OutboundMessage) -> None:
    p = Path(msg.file_path)
    if not p.exists():
        raise RuntimeError(f"send_document: file not found: {p}")

    boundary = "----AakaDocBoundary"
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    parts: list[bytes] = [_field(boundary, "chat_id", str(msg.recipient))]
    if msg.file_caption:
        parts.append(_field(boundary, "caption", msg.file_caption[:1024]))
    if msg.reply_to_message_id:
        parts.append(_field(boundary, "reply_to_message_id", str(msg.reply_to_message_id)))
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{p.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        + p.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    _post_multipart("sendDocument", parts, boundary, timeout=60, bot_id=msg.bot_id)


# ── Reaction ──────────────────────────────────────────────────────────────────

def send_reaction(msg: OutboundMessage) -> None:
    _post("setMessageReaction", {
        "chat_id": msg.recipient,
        "message_id": int(msg.reaction_message_id),
        "reaction": [{"type": "emoji", "emoji": msg.emoji}],
    }, timeout=5, bot_id=msg.bot_id)
