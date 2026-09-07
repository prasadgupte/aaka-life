#!/usr/bin/env python3
"""
sensor/scheduled_sender.py — Fires due scheduled_messages rows.

Runs every 60s via cron inside the sensor container.
Dispatches by channel:
  email     → skills.mail.gmail.send_email_to_member (uses /config/tokens/token_aakash.json)
  telegram  → gateway.egress.send(OutboundMessage)
  whatsapp  → gateway.egress.send(OutboundMessage)
  signal    → gateway.egress.send(OutboundMessage)  (needs signal-cli on this host —
              see SIGNAL_PLACEMENT in gateway/config.py; this cron runs on the VPS)
"""
import json
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(BASE))

from aaka_queue.queue import (
    get_due_scheduled_messages,
    mark_scheduled_sent,
    mark_scheduled_error,
)


def _send_email(payload: dict) -> dict:
    from skills.mail.gmail import send_email_to_member
    return send_email_to_member(
        to_member_id=payload["to_member_id"],
        subject=payload["subject"],
        body=payload["body"],
        html_body=payload.get("html_body", ""),
        from_member_id=payload.get("from_member_id", "aakash"),
    )


_CHAT_CHANNELS = ("telegram", "whatsapp", "signal")


def _send_chat(channel: str, payload: dict) -> dict:
    from gateway.egress import send
    from gateway.types import MessageKind, OutboundMessage

    recipient = payload.get("recipient") or payload.get("channel_id", "")
    msg = OutboundMessage(
        kind=MessageKind.TEXT,
        recipient=recipient,
        channel=channel,
        source="scheduled_sender",
        text=payload.get("text", ""),
        silent=payload.get("silent", False),
        reply_markup=payload.get("reply_markup"),
        reply_to_message_id=payload.get("reply_to_message_id"),
    )
    send(msg)
    return {"recipient": recipient}


def main() -> None:
    due = get_due_scheduled_messages(limit=50)
    if not due:
        return

    for msg in due:
        mid = msg["id"]
        channel = msg["channel"]
        try:
            payload = json.loads(msg["payload"])
            if channel == "email":
                result = _send_email(payload)
            elif channel in _CHAT_CHANNELS:
                result = _send_chat(channel, payload)
            else:
                raise ValueError(f"unknown channel: {channel!r}")
            mark_scheduled_sent(mid, result)
            print(f"[scheduled_sender] sent {mid} channel={channel}")
        except Exception as exc:
            mark_scheduled_error(mid, str(exc))
            print(f"[scheduled_sender] error {mid} channel={channel}: {exc}")


if __name__ == "__main__":
    main()
