#!/usr/bin/env python3
"""
sensor/wa_inbound.py — WhatsApp inbound receiver (Mac, localhost only).

Receives the wa-sidecar's POST /inbound, runs the message through the unified
ingress gateway, routes it via the sensor router, and sends the reply back out
through egress on the whatsapp channel — the same shape as the Telegram path,
with no OpenClaw in the loop.

Runs on the Mac alongside the executor (same process context as queue_worker.py,
where gateway.ingress + the router import cleanly). It does NOT run on the VPS
sensor and is only started when "whatsapp" ∈ ENABLED_CHANNELS.

Run:
  /Users/Shared/aaka-repo/venv/bin/python3 sensor/wa_inbound.py \\
      --host 127.0.0.1 --port 18793
  # or via uvicorn (launchd):
  uvicorn sensor.wa_inbound:app --host 127.0.0.1 --port 18793
"""
from __future__ import annotations

import argparse
import hmac
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402

from gateway.ingress import (  # noqa: E402
    receive as _ingress_receive_impl, neutralize_envelope,
    InboundMessage, Channel,
)
from gateway.egress import send as _egress_send_impl, OutboundMessage, MessageKind  # noqa: E402


# ── Seam wrappers (module-level so tests can patch them) ─────────────────────
# The router is imported lazily inside _route so importing this module (and its
# FastAPI app) stays cheap and side-effect-free for tests.

def _ingress_receive(msg: InboundMessage):
    # trust_envelope=False — raw_text here is the WhatsApp user's own message
    # body. Parsing it as a Format A/B envelope would let a sender forge
    # sender_id (and so pass the channel gate + every admin check). SEC-1.
    return _ingress_receive_impl(msg, trust_envelope=False)


def _route(text: str) -> str:
    from sensor.router_sensor import route as _route_impl
    return _route_impl(text)


def _wa_envelope(sender_id: str, channel_id: str, message_id: str, text: str,
                 sender_name: str = "") -> str:
    """Wrap a WhatsApp message in the Format A metadata envelope the router
    expects (mirrors telegram_poller._build_format_a). The 'whatsapp:' chat_id
    prefix tells gateway.ingress.normalize() to route on the whatsapp channel and
    reply to <jid>. Without this envelope route() has no sender/channel context
    and returns an empty reply.

    `text` is attacker-controlled, so it is passed through
    ingress.neutralize_envelope() first: a body that itself starts with an
    envelope marker gets an invisible separator prefixed so it cannot be parsed
    as a second (forged) envelope by the next hop. SEC-1."""
    import json as _json
    text = neutralize_envelope(text or "")
    meta = {
        "chat_id": f"whatsapp:{channel_id or sender_id}",
        "message_id": str(message_id or ""),
        "sender_id": str(sender_id),
        "sender_name": str(sender_name or ""),
        "conversation_label": f"id:{channel_id or sender_id}",
    }
    return (
        "Conversation info (untrusted metadata):\n"
        "```json\n"
        f"{_json.dumps(meta)}\n"
        "```\n"
        f"{text}"
    )


def _egress_send(msg: OutboundMessage) -> None:
    _egress_send_impl(msg)


app = FastAPI(title="Aaka WA Inbound Receiver")


def _shared_secret() -> str:
    """Optional shared secret for POST /inbound (SEC-7).

    Unset  → current behaviour (localhost trust, no header required).
    Set    → the sidecar must send `X-Aaka-Secret: <value>`; anything else 401.
    Set the SAME value for wa-sidecar (it reads $WA_INBOUND_SECRET too).
    """
    return os.environ.get("WA_INBOUND_SECRET", "").strip()


def _authorized(request: Request) -> bool:
    secret = _shared_secret()
    if not secret:
        return True
    return hmac.compare_digest(request.headers.get("X-Aaka-Secret", ""), secret)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/inbound")
async def inbound(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()

    # Sender / channel / message identity comes STRICTLY from the sidecar's JSON
    # fields — never from body["text"] (see _ingress_receive: trust_envelope=False).
    parsed = _ingress_receive(InboundMessage(
        raw_text=body.get("text", "") or "",
        sender_id=body.get("sender_id", "") or "",
        channel=Channel.WHATSAPP,
        channel_id=body.get("channel_id"),
        message_id=body.get("message_id"),
        media_path=body.get("media_path"),
        mime_type=body.get("mime_type"),
        timestamp=body.get("timestamp"),
        source="wa_inbound",
    ))

    if parsed is None:
        # Blocked sender or parse error — ingress already logged it.
        return Response(status_code=204)

    # Note: from_me messages ARE routed now — when aaka is linked to the user's own
    # number ("no spare phone"), their self-chat commands are fromMe. The sidecar
    # already suppresses aaka's OWN reply echoes by message-id, so this can't loop;
    # a stray echo that slips through isn't a valid command and route() returns "".

    # Route through the full sensor router with proper WhatsApp context. route()
    # re-normalizes this envelope (channel=whatsapp, channel_id=<jid>) and returns
    # the reply text for zero-token intents; queued intents return "" and are
    # handled by the executor.
    reply = _route(_wa_envelope(
        parsed.sender_id, parsed.channel_id, parsed.message_id or "", parsed.text,
        sender_name=body.get("push_name", "") or "",
    ))

    if reply:
        _egress_send(OutboundMessage(
            kind=MessageKind.TEXT,
            recipient=parsed.channel_id,
            channel="whatsapp",
            text=reply,
            source="wa_inbound",
            correlation_id=parsed.correlation_id,
        ))

    return JSONResponse({"ok": True})


def main() -> int:
    ap = argparse.ArgumentParser(description="Aaka WhatsApp inbound receiver")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WA_RECEIVER_PORT", "18793")))
    args = ap.parse_args()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
