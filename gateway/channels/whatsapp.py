"""
gateway/channels/whatsapp.py — WhatsApp channel adapter.

Delegates to gateway.backends.openclaw (OpenClawBackend).
Swap the backend by changing the import — no other code needs to change.
"""
from __future__ import annotations

from gateway.backends.openclaw import OpenClawBackend
from gateway.types import OutboundMessage

_backend = OpenClawBackend()


def send_text(msg: OutboundMessage) -> None:
    _backend.send_text(
        msg.recipient, msg.text,
        reply_to=msg.reply_to_message_id,
    )


def send_photo(msg: OutboundMessage) -> None:
    _backend.send_photo(
        msg.recipient, msg.photo_bytes or b"",
        caption=msg.photo_caption,
    )


def send_document(msg: OutboundMessage) -> None:
    _backend.send_document(
        msg.recipient, msg.file_path or "",
        caption=msg.file_caption,
    )


def send_reaction(msg: OutboundMessage) -> None:
    _backend.send_reaction(
        msg.recipient,
        msg.reaction_message_id or "",
        msg.emoji,
    )
