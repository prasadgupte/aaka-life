"""
Direct Telegram sender — emergency/debug utility only.

NOT used in normal flow. Normal flow: executor writes to outbox via write_outbox();
sensor flushes outbox via flush_outbox.py (cron).

Use this module only for emergency sends or debugging when the sensor is unreachable.
All sends still route through gateway.egress so they are audited and rate-limited.
"""
import json
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import aaka_config


def send(
    text: str,
    chat_id: str | None = None,
    *,
    reply_to_message_id: "str | int | None" = None,
) -> None:
    """Send a message via the egress gateway. Raises on failure."""
    if not chat_id:
        config_dir = os.environ.get("AAKA_CONFIG_DIR") or str(aaka_config.CONFIG_DIR)
        creds_path = Path(config_dir) / "tokens" / "message_send.json"
        creds = json.loads(creds_path.read_text())
        chat_id = creds.get("default_chat_id", "")
    if not chat_id:
        raise ValueError("No chat_id and no default_chat_id in message_send.json")

    from gateway.egress import send as _egress_send, OutboundMessage, MessageKind
    _egress_send(OutboundMessage(
        kind=MessageKind.TEXT,
        recipient=str(chat_id),
        channel="telegram",
        text=text,
        source="telegram_notify",
        reply_to_message_id=str(reply_to_message_id) if reply_to_message_id else None,
    ))
