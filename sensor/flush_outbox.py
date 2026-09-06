#!/usr/bin/env python3
"""Standalone outbox flusher — called by cron inside sensor container.

All sends route through gateway.egress so every delivery is audited,
rate-limited, and subject to the kill switch.
"""
import json, sys, os
sys.path.insert(0, os.environ.get("AAKA_BASE", "/app"))
from aaka_queue.queue import read_pending_outbox, mark_outbox_sent
from gateway.config import SIGNAL_PLACEMENT
from gateway.egress import send, OutboundMessage, MessageKind

# outbox `source` (the channel a message arrived on) → egress channel name.
# Anything unknown falls back to whatsapp, which is the historical default.
_CHANNEL_FOR_SOURCE = {
    "telegram": "telegram",
    "whatsapp": "whatsapp",
    "signal":   "signal",
    "slack":    "slack",
}

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

        channel = _CHANNEL_FOR_SOURCE.get(source, "whatsapp")

        # Signal rows are only flushable where the signal-cli daemon actually
        # runs. This flusher runs from cron on the VPS (sensor); with
        # SIGNAL_PLACEMENT=executor the daemon is on the Mac and POSTing to our
        # own loopback would fail every minute forever. Skip and say so — same
        # shape as the "web" branch above, which is also delivered elsewhere.
        if channel == "signal" and SIGNAL_PLACEMENT != "sensor":
            print(f"[skip] outbox item {item['id']}: signal placement="
                  f"{SIGNAL_PLACEMENT!r} — daemon is not on this host, leaving pending")
            continue

        # Special reaction entries: "__react:<emoji>:<message_id>"
        if text.startswith("__react:"):
            parts = text.split(":", 2)
            if len(parts) == 3 and source in ("telegram", "signal"):
                from gateway.egress import reaction
                send(reaction(channel_id, _CHANNEL_FOR_SOURCE[source], parts[1],
                              parts[2], source="flush_outbox"))
            mark_outbox_sent(item["id"], "sent")
            continue

        markup = item.get("reply_markup")
        if isinstance(markup, str):
            try:
                markup = json.loads(markup)
            except Exception:
                markup = None

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
