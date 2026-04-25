"""
gateway/channels — Channel adapter package.

Each module implements send functions for one outbound channel.
Add a new channel by creating gateway/channels/<name>.py with:
  send_text(msg: OutboundMessage) -> None
  send_photo(msg: OutboundMessage) -> None       # optional
  send_document(msg: OutboundMessage) -> None    # optional
  send_reaction(msg: OutboundMessage) -> None    # optional

Available adapters:
  telegram  — Telegram Bot API (direct HTTP, no CLI dependency)
  whatsapp  — OpenClaw/ZeroClaw CLI wrapper

To register a new channel in egress dispatch, add an entry in
gateway/egress.py → _CHANNEL_DISPATCH dict.
"""
