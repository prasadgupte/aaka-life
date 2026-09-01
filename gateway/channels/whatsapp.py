"""
gateway/channels/whatsapp.py — WhatsApp channel adapter.

Posts to the wa-sidecar HTTP API (wa-sidecar/index.js) instead of shelling out
to the OpenClaw subprocess. The sidecar owns the Baileys WhatsApp Web session.

Interface is unchanged — egress.py dispatches to send_text / send_photo /
send_document / send_reaction exactly as before, so gateway/egress.py needs no
modification. stdlib only (urllib.request) — no new dependencies.
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.request

from gateway.config import WA_SIDECAR_PORT
from gateway.types import OutboundMessage

_SIDECAR_BASE = f"http://127.0.0.1:{WA_SIDECAR_PORT}"


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{_SIDECAR_BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def send_text(msg: OutboundMessage) -> None:
    _post("/send", {"jid": msg.recipient, "text": msg.text})


def send_photo(msg: OutboundMessage) -> None:
    # photo_bytes arrive as bytes; the sidecar reads media from a file path, so
    # stage them in a temp file for /send-media, then clean up.
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(msg.photo_bytes or b"")
        tmp = f.name
    try:
        _post("/send-media", {
            "jid": msg.recipient,
            "mime_type": "image/jpeg",
            "file_path": tmp,
            "caption": msg.photo_caption or "",
        })
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def send_document(msg: OutboundMessage) -> None:
    _post("/send-media", {
        "jid": msg.recipient,
        "mime_type": "application/octet-stream",
        "file_path": msg.file_path or "",
        "caption": msg.file_caption or "",
    })


def send_reaction(msg: OutboundMessage) -> None:
    # WhatsApp reactions are not wired in v1. No-op keeps the egress dispatch
    # contract intact (egress calls send_reaction for REACTION kind messages).
    return None
