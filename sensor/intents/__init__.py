"""
sensor/intents — intent handler package.

Each domain module exports:
  HANDLES: frozenset[str]   — the intent names it handles
  handle(intent, message, sender, channel_id, source) -> str

RoutingContext is available for handlers that need the full request context
(currently used by queue-writing intents in router_sensor.py; local handlers
receive individual parameters for backward compatibility).
"""
from dataclasses import dataclass


@dataclass
class RoutingContext:
    """Immutable snapshot of the routing context for a single inbound message."""
    sender_id: str
    channel_id: str
    source: str               # "telegram" | "whatsapp"
    dry_run: bool = False
    message_id: "str | None" = None
    sender_email: str = ""
    media: "dict | None" = None
    correlation_id: "str | None" = None

    # Derived fields — filled by router after member lookup
    sender_member: "dict | None" = None
    sender_member_id: "str | None" = None
    is_admin: bool = False
    namespace: "str | None" = None
