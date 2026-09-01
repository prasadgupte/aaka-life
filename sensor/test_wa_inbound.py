#!/usr/bin/env python3
"""
sensor/test_wa_inbound.py — unit tests for the WhatsApp inbound receiver.

No network: uses starlette/fastapi TestClient against the imported FastAPI app,
with ingress.receive / router.route / egress.send mocked out. Confirms the
receiver maps the sidecar's POST /inbound body onto an InboundMessage with
channel=whatsapp, routes it, and patches is_self_dm from from_me.

Run: /Users/Shared/aaka-repo/venv/bin/python3 sensor/test_wa_inbound.py
     (exit 0 = pass)
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Isolate config so nothing touches live data on import.
_TMP_CONFIG = Path(tempfile.mkdtemp(prefix="aaka-wa-inbound-test-"))
(_TMP_CONFIG / "logs").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AAKA_CONFIG_DIR", str(_TMP_CONFIG))

from starlette.testclient import TestClient  # noqa: E402

import sensor.wa_inbound as wa  # noqa: E402

client = TestClient(wa.app)

_FAILURES = []


def check(desc, cond):
    if cond:
        print(f"  ok   {desc}")
    else:
        print(f"  FAIL {desc}")
        _FAILURES.append(desc)


def test_inbound_calls_ingress_receive():
    """POST /inbound -> ingress.receive() called with channel=whatsapp."""
    mock_parsed = MagicMock()
    mock_parsed.text = "/status"
    mock_parsed.sender_id = "4915123@s.whatsapp.net"
    mock_parsed.channel_id = "4915123@s.whatsapp.net"
    mock_parsed.is_self_dm = False

    with patch("sensor.wa_inbound._ingress_receive", return_value=mock_parsed) as mock_recv, \
         patch("sensor.wa_inbound._route", return_value="[aaka] ok"), \
         patch("sensor.wa_inbound._egress_send"):
        r = client.post("/inbound", json={
            "sender_id": "4915123@s.whatsapp.net",
            "channel_id": "4915123@s.whatsapp.net",
            "message_id": "abc123",
            "text": "/status",
            "from_me": False,
            "timestamp": "2026-09-01T10:00:00Z",
        })
    check("POST /inbound returns 200 on routed message", r.status_code == 200)
    inbound = mock_recv.call_args[0][0]  # first positional arg = InboundMessage
    ch = inbound.channel
    ch_val = ch.value if hasattr(ch, "value") else str(ch)
    check("ingress.receive called with channel=whatsapp", ch_val == "whatsapp")
    check("ingress.receive got sender_id", inbound.sender_id == "4915123@s.whatsapp.net")
    check("ingress.receive got raw_text", inbound.raw_text == "/status")


def test_inbound_blocked_sender_returns_204():
    """If ingress.receive() returns None (blocked/parse error), receiver returns 204."""
    with patch("sensor.wa_inbound._ingress_receive", return_value=None):
        r = client.post("/inbound", json={
            "sender_id": "blocked@s.whatsapp.net",
            "channel_id": "blocked@s.whatsapp.net",
            "message_id": "x", "text": "hi", "from_me": False,
            "timestamp": "2026-09-01T10:00:00Z",
        })
    check("blocked sender -> 204", r.status_code == 204)


def test_from_me_is_routed():
    """from_me=True -> IS routed now (self-chat when aaka is linked to the user's
    own number). The sidecar suppresses aaka's own reply echoes by id, so there's
    no loop; wa_inbound must not drop self-chat commands."""
    mock_parsed = MagicMock()
    mock_parsed.text = "status"
    mock_parsed.sender_id = "me@s.whatsapp.net"
    mock_parsed.channel_id = "me@s.whatsapp.net"
    routed = {"called": False}
    egressed = {"called": False}
    def _mark_route(_):
        routed["called"] = True
        return "🟢 healthy"
    def _mark_egress(_):
        egressed["called"] = True
    with patch("sensor.wa_inbound._ingress_receive", return_value=mock_parsed), \
         patch("sensor.wa_inbound._route", side_effect=_mark_route), \
         patch("sensor.wa_inbound._egress_send", side_effect=_mark_egress):
        r = client.post("/inbound", json={
            "sender_id": "me@s.whatsapp.net", "channel_id": "me@s.whatsapp.net",
            "message_id": "y", "text": "status", "from_me": True,
            "timestamp": "2026-09-01T10:00:00Z",
        })
    check("from_me=True -> 200", r.status_code == 200)
    check("from_me=True -> routed (self-chat)", routed["called"] is True)
    check("from_me=True -> reply sent", egressed["called"] is True)


def test_reply_sent_via_egress():
    """A non-empty route() reply is sent back through egress on the whatsapp channel."""
    mock_parsed = MagicMock()
    mock_parsed.text = "/status"
    mock_parsed.sender_id = "4915123@s.whatsapp.net"
    mock_parsed.channel_id = "4915123@s.whatsapp.net"
    mock_parsed.is_self_dm = False
    with patch("sensor.wa_inbound._ingress_receive", return_value=mock_parsed), \
         patch("sensor.wa_inbound._route", return_value="[aaka] health ok"), \
         patch("sensor.wa_inbound._egress_send") as mock_send:
        client.post("/inbound", json={
            "sender_id": "4915123@s.whatsapp.net",
            "channel_id": "4915123@s.whatsapp.net",
            "message_id": "z", "text": "/status", "from_me": False,
            "timestamp": "2026-09-01T10:00:00Z",
        })
    check("egress.send called once for the reply", mock_send.call_count == 1)
    if mock_send.call_count == 1:
        out = mock_send.call_args[0][0]
        check("reply sent on whatsapp channel", out.channel == "whatsapp")
        check("reply addressed to channel_id", out.recipient == "4915123@s.whatsapp.net")


def test_health():
    r = client.get("/health")
    check("GET /health -> 200", r.status_code == 200)
    check("GET /health -> {ok:true}", r.json() == {"ok": True})


def main():
    test_inbound_calls_ingress_receive()
    test_inbound_blocked_sender_returns_204()
    test_from_me_is_routed()
    test_reply_sent_via_egress()
    test_health()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all wa_inbound receiver checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
