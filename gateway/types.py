"""
gateway/types.py — Shared types for outbound messages.

Defined here (not in egress.py) so channel adapters can import
OutboundMessage without creating a circular dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class MessageKind(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    DOCUMENT = "document"
    REACTION = "reaction"


@dataclass
class OutboundMessage:
    kind: MessageKind
    recipient: str          # chat_id (TG), E.164 phone (WA), or email address
    channel: str            # "telegram" | "whatsapp" | "email"
    source: str             # caller name for audit log, e.g. "flush_outbox"

    # TEXT
    text: str = ""
    reply_to_message_id: Optional[str] = None
    silent: bool = False
    reply_markup: Optional[dict] = None  # Telegram InlineKeyboardMarkup

    # Multi-bot: which bot sends/receives this. None → default/single bot.
    bot_id: Optional[str] = None

    # PHOTO
    photo_bytes: Optional[bytes] = None
    photo_caption: str = ""

    # DOCUMENT
    file_path: Optional[str] = None     # local path
    file_caption: str = ""

    # REACTION
    emoji: str = ""
    reaction_message_id: Optional[str] = None

    # Correlation — set by ingress.py when processing an inbound message
    correlation_id: Optional[str] = None
