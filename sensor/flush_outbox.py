#!/usr/bin/env python3
"""Standalone outbox flusher — called by cron inside sensor container.

All sends route through gateway.egress so every delivery is audited,
rate-limited, and subject to the kill switch.
"""
import json, sys, os
sys.path.insert(0, os.environ.get("AAKA_BASE", "/app"))
from aaka_queue.queue import read_pending_outbox, mark_outbox_sent
from gateway.egress import send, OutboundMessage, MessageKind

items = read_pending_outbox()
for item in items:
    try:
        text = item["text"]
        source = item.get("source", "telegram")
        channel_id = item["channel_id"]

        # Web channel — delivered out-of-band by executor/webui/server.py
        # over SSE. Never send web rows via telegram/whatsapp egress.
        if source == "web":
            continue

        # Special reaction entries: "__react:<emoji>:<message_id>"
        if text.startswith("__react:"):
            parts = text.split(":", 2)
            if len(parts) == 3 and source == "telegram":
                from gateway.egress import reaction
                send(reaction(channel_id, "telegram", parts[1], parts[2],
                              source="flush_outbox"))
            mark_outbox_sent(item["id"], "sent")
            continue

        markup = item.get("reply_markup")
        if isinstance(markup, str):
            try:
                markup = json.loads(markup)
            except Exception:
                markup = None

        channel = "telegram" if source == "telegram" else "whatsapp"
        send(OutboundMessage(
            kind=MessageKind.TEXT,
            recipient=channel_id,
            channel=channel,
            text=text,
            source="flush_outbox",
            reply_to_message_id=item.get("reply_to_message_id"),
            silent=bool(item.get("silent")),
            reply_markup=markup,
        ))
        mark_outbox_sent(item["id"], "sent")
    except Exception as e:
        print(f"[error] outbox item {item['id']}: {e}")
        mark_outbox_sent(item["id"], "error")

print(f"flushed {len(items)} outbox items")
