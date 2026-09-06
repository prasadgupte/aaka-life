#!/usr/bin/env python3
"""
gateway/channels/signal_cli_test.py — Signal adapter tests against a MOCK
signal-cli daemon (stdlib http.server). No network, no signal-cli, no account.

The mock speaks the real contract from the signal-cli man page
(`signal-cli-jsonrpc.5`): POST /api/v1/rpc takes a JSON-RPC 2.0 request and
answers {"jsonrpc":"2.0","id":…,"result":{…}} or an {"error":…}; GET
/api/v1/check is 200. Every request it sees is recorded so the tests can assert
the exact params the adapter sent.

Run: /Users/Shared/aaka-repo/venv/bin/python3 gateway/channels/signal_cli_test.py
     (exit 0 = pass)
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_TMP_CONFIG = Path(tempfile.mkdtemp(prefix="aaka-signal-test-"))
(_TMP_CONFIG / "data").mkdir(parents=True, exist_ok=True)
(_TMP_CONFIG / "logs").mkdir(parents=True, exist_ok=True)
os.environ["AAKA_CONFIG_DIR"] = str(_TMP_CONFIG)
os.environ["SIGNAL_ACCOUNT"] = "+491700000000"

from gateway.types import MessageKind, OutboundMessage  # noqa: E402

_FAILURES = []


def check(desc, cond):
    print(("  ok   " if cond else "  FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


# ── Mock signal-cli daemon ────────────────────────────────────────────────────

class _MockDaemon(BaseHTTPRequestHandler):
    requests: list = []
    fail_next: dict = {}       # method → error message

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        if self.path == "/api/v1/check":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/api/v1/rpc":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        _MockDaemon.requests.append(req)
        method = req.get("method")
        if method in _MockDaemon.fail_next:
            body = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -1, "message": _MockDaemon.fail_next[method]}}
        elif method == "version":
            body = {"jsonrpc": "2.0", "id": req.get("id"), "result": {"version": "0.13.4"}}
        elif method == "listGroups":
            body = {"jsonrpc": "2.0", "id": req.get("id"),
                    "result": [{"id": "Z3JvdXA=", "name": "Family"}]}
        else:
            body = {"jsonrpc": "2.0", "id": req.get("id"), "result": {"timestamp": 1735000000123}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _start_daemon():
    srv = HTTPServer(("127.0.0.1", 0), _MockDaemon)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["SIGNAL_CLI_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv


def _sent(method="send"):
    """Params of the last request for `method` (or None)."""
    for req in reversed(_MockDaemon.requests):
        if req.get("method") == method:
            return req.get("params", {})
    return None


def _all(method="send"):
    return [r.get("params", {}) for r in _MockDaemon.requests if r.get("method") == method]


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_send_text_dm():
    from gateway.channels import signal_cli as sig
    _MockDaemon.requests.clear()
    sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="+15550000000",
                                  channel="signal", text="hello", source="t"))
    p = _sent()
    check("send_text → recipient is a list", p.get("recipient") == ["+15550000000"])
    check("send_text → message body", p.get("message") == "hello")
    check("send_text → account injected from SIGNAL_ACCOUNT",
          p.get("account") == "+491700000000")
    check("send_text → no groupId for a DM", "groupId" not in p)
    req = _MockDaemon.requests[-1]
    check("request is JSON-RPC 2.0 with an id",
          req.get("jsonrpc") == "2.0" and bool(req.get("id")))


def test_send_text_group():
    from gateway.channels import signal_cli as sig
    _MockDaemon.requests.clear()
    sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="group:Z3JvdXA=",
                                  channel="signal", text="hi all", source="t"))
    p = _sent()
    check("group send → groupId without the prefix", p.get("groupId") == "Z3JvdXA=")
    check("group send → no recipient list", "recipient" not in p)


def test_quote_timestamp():
    from gateway.channels import signal_cli as sig
    _MockDaemon.requests.clear()
    sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="+15550000000",
                                  channel="signal", text="re", source="t",
                                  reply_to_message_id="1735000000000"))
    p = _sent()
    check("bare timestamp → quoteTimestamp", p.get("quoteTimestamp") == 1735000000000)

    _MockDaemon.requests.clear()
    sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="group:Z3JvdXA=",
                                  channel="signal", text="re", source="t",
                                  reply_to_message_id="+15550000000:1735000000001"))
    p = _sent()
    check("author:timestamp → quoteAuthor + quoteTimestamp",
          p.get("quoteAuthor") == "+15550000000" and p.get("quoteTimestamp") == 1735000000001)

    _MockDaemon.requests.clear()
    sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="group:Z3JvdXA=",
                                  channel="signal", text="re", source="t",
                                  reply_to_message_id="1735000000002"))
    p = _sent()
    check("group quote without an author is dropped, not sent broken",
          "quoteTimestamp" not in p and p.get("message") == "re")


def test_send_photo_and_document():
    from gateway.channels import signal_cli as sig
    _MockDaemon.requests.clear()
    sig.send_photo(OutboundMessage(kind=MessageKind.PHOTO, recipient="+15550000000",
                                   channel="signal", photo_bytes=b"\xff\xd8jpeg",
                                   photo_caption="a pic", source="t"))
    p = _sent()
    check("send_photo → attachments is a list of one path",
          isinstance(p.get("attachments"), list) and len(p["attachments"]) == 1)
    check("send_photo → caption becomes the message", p.get("message") == "a pic")
    check("send_photo → temp file is cleaned up",
          not Path(p["attachments"][0]).exists())

    doc = _TMP_CONFIG / "report.pdf"
    doc.write_bytes(b"%PDF-1.4 test")
    _MockDaemon.requests.clear()
    sig.send_document(OutboundMessage(kind=MessageKind.DOCUMENT, recipient="+15550000000",
                                      channel="signal", file_path=str(doc),
                                      file_caption="report", source="t"))
    p = _sent()
    check("send_document → attaches the real path", p.get("attachments") == [str(doc)])
    check("send_document → caption forwarded", p.get("message") == "report")

    missing = False
    try:
        sig.send_document(OutboundMessage(kind=MessageKind.DOCUMENT, recipient="+15550000000",
                                          channel="signal", file_path="/nope/none.pdf",
                                          source="t"))
    except RuntimeError:
        missing = True
    check("send_document raises on a missing file", missing)


def test_send_reaction():
    from gateway.channels import signal_cli as sig
    _MockDaemon.requests.clear()
    sig.send_reaction(OutboundMessage(kind=MessageKind.REACTION, recipient="+15550000000",
                                      channel="signal", emoji="👀",
                                      reaction_message_id="1735000000000", source="t"))
    p = _sent("sendReaction")
    check("reaction → emoji", p.get("emoji") == "👀")
    check("reaction → targetTimestamp int", p.get("targetTimestamp") == 1735000000000)
    check("reaction → DM author defaults to the recipient",
          p.get("targetAuthor") == "+15550000000")

    _MockDaemon.requests.clear()
    sig.send_reaction(OutboundMessage(kind=MessageKind.REACTION, recipient="group:Z3JvdXA=",
                                      channel="signal", emoji="👀",
                                      reaction_message_id="+15550000001:1735000000009",
                                      source="t"))
    p = _sent("sendReaction")
    check("group reaction → targetAuthor from the id",
          p.get("targetAuthor") == "+15550000001" and p.get("groupId") == "Z3JvdXA=")

    _MockDaemon.requests.clear()
    sig.send_reaction(OutboundMessage(kind=MessageKind.REACTION, recipient="+15550000000",
                                      channel="signal", emoji="👀",
                                      reaction_message_id="", source="t"))
    check("reaction with no target sends nothing", _all("sendReaction") == [])


def test_options_render_and_resolve():
    from gateway.channels import signal_cli as sig
    _MockDaemon.requests.clear()
    markup = {"inline_keyboard": [[{"text": "Yes", "callback_data": "yes"},
                                   {"text": "No", "callback_data": "no"}]]}
    sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="+15550000000",
                                  channel="signal", text="Confirm?", source="t",
                                  reply_markup=markup))
    body = _sent().get("message", "")
    check("keyboard → numbered list in the body",
          "1. Yes" in body and "2. No" in body and "Reply with a number" in body)
    check("options file written",
          (_TMP_CONFIG / "data" / "signal_options.json").exists())
    check("bare '2' resolves to the second callback_data",
          sig.resolve_option("+15550000000", "2") == "no")
    check("options are consumed — a second '2' does not resolve",
          sig.resolve_option("+15550000000", "2") is None)
    check("non-numeric text never resolves",
          sig.resolve_option("+15550000000", "hello") is None)

    # Out-of-range and unknown chats are safe.
    sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="+15550000000",
                                  channel="signal", text="Pick", source="t",
                                  reply_markup=markup))
    check("out-of-range number does not resolve",
          sig.resolve_option("+15550000000", "9") is None)
    check("unknown chat does not resolve",
          sig.resolve_option("+15559999999", "1") is None)


def test_split_long_text():
    from gateway.channels import signal_cli as sig
    _MockDaemon.requests.clear()
    long_text = "\n".join(f"line {i}" for i in range(500))
    sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="+15550000000",
                                  channel="signal", text=long_text, source="t",
                                  reply_to_message_id="1735000000000"))
    sends = _all()
    check("long text is split into several sends", len(sends) > 1)
    check("every chunk is within the Signal body limit",
          all(len(p["message"]) <= sig._MAX_LEN for p in sends))
    check("only the first chunk quotes",
          "quoteTimestamp" in sends[0] and all("quoteTimestamp" not in p for p in sends[1:]))
    check("no content is lost",
          "line 0" in sends[0]["message"] and "line 499" in sends[-1]["message"])


def test_status_and_errors():
    from gateway.channels import signal_cli as sig
    st = sig.status(timeout=3)
    check("status() reports ok + version", st["ok"] and st["version"] == "0.13.4")
    check("status() carries the account", st["account"] == "+491700000000")

    check("list_groups() returns the daemon's groups",
          [g["id"] for g in sig.list_groups()] == ["Z3JvdXA="])

    _MockDaemon.fail_next["send"] = "Unregistered user"
    raised = ""
    try:
        sig.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="+15550000001",
                                      channel="signal", text="x", source="t"))
    except RuntimeError as exc:
        raised = str(exc)
    _MockDaemon.fail_next.clear()
    check("a JSON-RPC error is surfaced as RuntimeError", "Unregistered user" in raised)

    old = os.environ["SIGNAL_CLI_URL"]
    os.environ["SIGNAL_CLI_URL"] = "http://127.0.0.1:1"   # nothing listening
    st = sig.status(timeout=1)
    check("status() on a dead daemon is ok=False with an error",
          st["ok"] is False and st["error"])
    os.environ["SIGNAL_CLI_URL"] = old


def test_egress_registration():
    import gateway.egress as egress
    check("egress: signal channel registered", "signal" in egress._CHANNEL_DISPATCH)
    check("egress: signal resolves to the signal_cli module",
          egress._CHANNEL_DISPATCH["signal"]().__name__.endswith("signal_cli"))
    from gateway.ingress import Channel
    check("ingress: Channel.SIGNAL exists", Channel.SIGNAL.value == "signal")


def test_module_does_not_shadow_stdlib_signal():
    """A gateway/channels/signal.py would shadow stdlib `signal` for anything run
    from that directory — the module must stay signal_cli.py."""
    check("no gateway/channels/signal.py exists",
          not (REPO_ROOT / "gateway" / "channels" / "signal.py").exists())
    import signal as stdlib_signal
    check("stdlib signal still resolves to the standard library",
          hasattr(stdlib_signal, "SIGTERM"))


def main():
    srv = _start_daemon()
    try:
        test_send_text_dm()
        test_send_text_group()
        test_quote_timestamp()
        test_send_photo_and_document()
        test_send_reaction()
        test_options_render_and_resolve()
        test_split_long_text()
        test_status_and_errors()
        test_egress_registration()
        test_module_does_not_shadow_stdlib_signal()
    finally:
        srv.shutdown()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all signal channel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
