#!/usr/bin/env python3
"""
gateway/channels/test_whatsapp_channel.py — unit tests for the rewired WA channel.

Confirms gateway/channels/whatsapp.py POSTs to the wa-sidecar HTTP API instead of
shelling out to OpenClaw. No network, no subprocesses: urllib.request.urlopen is
mocked. Signatures (send_text/send_photo/send_document/send_reaction) are unchanged.

Run: /Users/Shared/aaka-repo/venv/bin/python3 gateway/channels/test_whatsapp_channel.py
     (exit 0 = pass)
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from gateway.types import OutboundMessage, MessageKind  # noqa: E402

_FAILURES = []


def check(desc, cond):
    if cond:
        print(f"  ok   {desc}")
    else:
        print(f"  FAIL {desc}")
        _FAILURES.append(desc)


def _mock_urlopen_response(payload=b'{"ok": true}'):
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_send_text_posts_to_sidecar_no_subprocess():
    """send_text() issues exactly one POST /send, zero subprocess.run calls."""
    msg = OutboundMessage(
        kind=MessageKind.TEXT, recipient="4915123@s.whatsapp.net",
        channel="whatsapp", source="test", text="hello",
    )
    resp = _mock_urlopen_response()
    with patch("urllib.request.urlopen", return_value=resp) as mock_url, \
         patch("subprocess.run") as mock_sub:
        from gateway.channels.whatsapp import send_text
        send_text(msg)

    check("send_text -> exactly one urlopen", mock_url.call_count == 1)
    if mock_url.call_count == 1:
        req = mock_url.call_args[0][0]
        check("send_text POSTs to /send", req.get_full_url().endswith("/send"))
        body = json.loads(req.data)
        check("send_text body has jid", body.get("jid") == "4915123@s.whatsapp.net")
        check("send_text body has text", body.get("text") == "hello")
    check("send_text spawns zero subprocesses", mock_sub.call_count == 0)


def test_send_photo_posts_to_send_media():
    """send_photo() POSTs to /send-media with mime_type=image/jpeg."""
    msg = OutboundMessage(
        kind=MessageKind.PHOTO, recipient="4915123@s.whatsapp.net",
        channel="whatsapp", source="test", photo_bytes=b"\xff\xd8", photo_caption="hi",
    )
    resp = _mock_urlopen_response()
    with patch("urllib.request.urlopen", return_value=resp) as mock_url, \
         patch("subprocess.run") as mock_sub:
        from gateway.channels.whatsapp import send_photo
        send_photo(msg)

    check("send_photo -> at least one urlopen", mock_url.call_count == 1)
    if mock_url.call_count == 1:
        req = mock_url.call_args[0][0]
        check("send_photo POSTs to /send-media", req.get_full_url().endswith("/send-media"))
        body = json.loads(req.data)
        check("send_photo mime_type image/jpeg", body.get("mime_type") == "image/jpeg")
        check("send_photo caption forwarded", body.get("caption") == "hi")
    check("send_photo spawns zero subprocesses", mock_sub.call_count == 0)


def test_send_document_posts_to_send_media():
    """send_document() POSTs file_path/caption to /send-media, no subprocess."""
    msg = OutboundMessage(
        kind=MessageKind.DOCUMENT, recipient="4915123@s.whatsapp.net",
        channel="whatsapp", source="test", file_path="/tmp/report.pdf",
        file_caption="report",
    )
    resp = _mock_urlopen_response()
    with patch("urllib.request.urlopen", return_value=resp) as mock_url, \
         patch("subprocess.run") as mock_sub:
        from gateway.channels.whatsapp import send_document
        send_document(msg)

    check("send_document -> one urlopen", mock_url.call_count == 1)
    if mock_url.call_count == 1:
        req = mock_url.call_args[0][0]
        check("send_document POSTs to /send-media", req.get_full_url().endswith("/send-media"))
        body = json.loads(req.data)
        check("send_document forwards file_path", body.get("file_path") == "/tmp/report.pdf")
    check("send_document spawns zero subprocesses", mock_sub.call_count == 0)


def test_no_openclaw_import():
    """gateway/channels/whatsapp.py must not import openclaw."""
    import sys as _sys
    for k in list(_sys.modules.keys()):
        if "channels.whatsapp" in k or "backends.openclaw" in k:
            del _sys.modules[k]
    import gateway.channels.whatsapp as wa_mod
    check("no 'openclaw' symbol in module namespace",
          "openclaw" not in dir(wa_mod))
    check("no OpenClawBackend class referenced",
          not any(isinstance(v, type) and "OpenClaw" in v.__name__
                  for v in vars(wa_mod).values()))
    src = (REPO_ROOT / "gateway" / "channels" / "whatsapp.py").read_text()
    check("whatsapp.py source has no 'openclaw'", "openclaw" not in src)


def main():
    test_send_text_posts_to_sidecar_no_subprocess()
    test_send_photo_posts_to_send_media()
    test_send_document_posts_to_send_media()
    test_no_openclaw_import()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all whatsapp channel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
