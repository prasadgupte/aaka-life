"""
gateway/channels/slack.py — Slack Web API channel adapter (outbound).

Implements the channel interface (send_text, send_photo, send_document,
send_reaction) used by gateway.egress. No OpenClaw, no third-party SDK — plain
HTTPS to the Slack Web API with a bot token.

Token: SLACK_BOT_TOKEN env → $AAKA_CONFIG_DIR/tokens/slack.json ({"bot_token": ...}).
Multi-workspace: SLACK_BOT_TOKEN_<ID> env → tokens/slack_workspaces.json ({"<id>": token}),
selected by OutboundMessage.bot_id — same pattern as Telegram multi-bot.

Inbound (Socket Mode) is a separate poller — see docs/openclaw-removal.md §4.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from gateway.types import OutboundMessage

_SLACK_API = "https://slack.com/api/"
_token_cache: str = ""
_token_mtime: float = 0.0
_ws_cache: "dict[str, str]" = {}
_ws_mtime: float = 0.0
_TTL = 60.0


def _load_workspaces() -> "dict[str, str]":
    global _ws_cache, _ws_mtime
    now = time.monotonic()
    if _ws_cache and now - _ws_mtime < _TTL:
        return _ws_cache
    path = Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "tokens" / "slack_workspaces.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                _ws_cache = {k: str(v) for k, v in data.items() if v}
                _ws_mtime = now
        except Exception:
            pass
    return _ws_cache


def bot_token(bot_id: "str | None" = None) -> str:
    global _token_cache, _token_mtime
    if bot_id:
        env = os.environ.get("SLACK_BOT_TOKEN_" + bot_id.upper().replace("-", "_"), "")
        if env:
            return env
        tok = _load_workspaces().get(bot_id, "")
        if tok:
            return tok
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if token:
        return token
    now = time.monotonic()
    if _token_cache and now - _token_mtime < _TTL:
        return _token_cache
    creds = Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "tokens" / "slack.json"
    if creds.exists():
        try:
            _token_cache = json.loads(creds.read_text()).get("bot_token", "")
            _token_mtime = now
        except Exception:
            pass
    return _token_cache


def _post(method: str, payload: dict, bot_id: "str | None" = None, timeout: int = 15) -> dict:
    token = bot_token(bot_id)
    if not token:
        raise RuntimeError("No Slack bot token available")
    req = urllib.request.Request(
        _SLACK_API + method,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Slack {method} HTTP {exc.code}: {exc.read()[:200]}") from exc
    if not resp.get("ok"):
        raise RuntimeError(f"Slack {method} error: {resp.get('error', resp)}")
    return resp


def send_text(msg: OutboundMessage) -> None:
    payload = {"channel": msg.recipient, "text": msg.text}
    if msg.reply_to_message_id:  # Slack thread_ts
        payload["thread_ts"] = str(msg.reply_to_message_id)
    _post("chat.postMessage", payload, bot_id=msg.bot_id)


def send_photo(msg: OutboundMessage) -> None:
    # Slack's file upload is a multi-step external-upload flow; until it's wired,
    # post the caption as text so the channel stays usable. See §4 of the runbook.
    caption = msg.photo_caption or "[photo]"
    _post("chat.postMessage", {"channel": msg.recipient, "text": caption}, bot_id=msg.bot_id)


def send_document(msg: OutboundMessage) -> None:
    caption = msg.file_caption or f"[file: {Path(msg.file_path).name if msg.file_path else 'document'}]"
    _post("chat.postMessage", {"channel": msg.recipient, "text": caption}, bot_id=msg.bot_id)


def send_reaction(msg: OutboundMessage) -> None:
    _post("reactions.add", {
        "channel": msg.recipient,
        "timestamp": str(msg.reaction_message_id),
        "name": (msg.emoji or "eyes").strip(":"),
    }, bot_id=msg.bot_id)
