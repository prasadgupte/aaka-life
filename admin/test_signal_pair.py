#!/usr/bin/env python3
"""
admin/test_signal_pair.py — unit tests for the Signal pairing page.

Offline: signal-cli is never invoked. The linker thread is the part that shells
out, so these drive the HTTP surface and the QR helper directly.

Run: python3 admin/test_signal_pair.py   (exit 0 = pass)
"""
import json
import sys
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "admin"))

import signal_pair as sp  # noqa: E402

_FAILURES = []


def check(desc, cond):
    if cond:
        print(f"  ok   {desc}")
    else:
        print(f"  FAIL {desc}")
        _FAILURES.append(desc)


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), sp.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_qr_helper():
    uri = "sgnl://linkdevice?uuid=abc&pub_key=def"
    data = sp._qr_data_uri(uri)
    # qrcode is a declared dependency, but degrade gracefully if absent.
    if data:
        check("QR helper returns a PNG data URI", data.startswith("data:image/png;base64,"))
        check("QR payload is non-trivial", len(data) > 200)
    else:
        check("QR helper degrades to empty string without qrcode", data == "")


def test_endpoints_reflect_state():
    srv, base = _serve()
    try:
        sp._set(status="starting", account="", message="", uri="", qr="")
        code, body = _get(base + "/status")
        check("/status is 200", code == 200)
        check("/status reports starting", json.loads(body)["status"] == "starting")

        sp._set(status="qr", uri="sgnl://x", qr="data:image/png;base64,AAA")
        code, body = _get(base + "/qr")
        payload = json.loads(body)
        check("/qr returns the data URI", payload["qr"].startswith("data:image/png"))
        check("/qr also returns the raw uri", payload["uri"] == "sgnl://x")

        sp._set(status="linked", account="+15550000000")
        _, body = _get(base + "/status")
        check("/status reports the linked account",
              json.loads(body)["account"] == "+15550000000")

        code, body = _get(base + "/")
        check("page is served", code == 200 and b"Link Signal" in body)
        check("page warns that linking shares the operator's account",
              b"reply as you" in body)
        check("page polls /status", b"/status" in body)
    finally:
        srv.shutdown()


def test_unknown_path_404s():
    srv, base = _serve()
    try:
        try:
            _get(base + "/nope")
            check("unknown path 404s", False)
        except urllib.error.HTTPError as e:  # noqa: F821
            check("unknown path 404s", e.code == 404)
    finally:
        srv.shutdown()


def test_binds_loopback_only():
    """The link URI is a credential — anyone who scans it gets a device on the
    account — so the server must never listen on a routable address."""
    src = (REPO_ROOT / "admin" / "signal_pair.py").read_text()
    check("server binds 127.0.0.1", 'HTTPServer(("127.0.0.1"' in src)
    check("no 0.0.0.0 bind", "0.0.0.0" not in src)


def main():
    test_qr_helper()
    test_endpoints_reflect_state()
    test_unknown_path_404s()
    test_binds_loopback_only()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all signal pairing-page checks passed")
    return 0


if __name__ == "__main__":
    import urllib.error  # noqa: F401  (used in test_unknown_path_404s)
    sys.exit(main())
