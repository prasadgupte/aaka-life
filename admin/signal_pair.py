#!/usr/bin/env python3
"""
admin/signal_pair.py — browser pairing page for the Signal channel.

The same idea as the WhatsApp sidecar's page (wa-sidecar/index.js): rather than
printing a QR into a terminal that the user has to scan before it expires, serve
a small local page that always shows the *current* code and reports live status.

Signal's `link` URI expires after a couple of minutes. The terminal flow made
that the user's problem — miss the window and you start over. Here the link
subprocess is simply restarted when it times out, so the page always shows a
scannable code, which is what the WhatsApp page achieves with its rotating QR.

Endpoints (all loopback):
    GET /         the page
    GET /status   {"status": starting|qr|linked|error, "account": ..., "message": ...}
    GET /qr       {"qr": "data:image/png;base64,…", "uri": "sgnl://…"}

Run:
    python3 admin/signal_pair.py [--port 18795] [--name aaka] [--no-open]

Exits 0 once the device is linked, non-zero on an unrecoverable error, so a
setup script can just wait on it.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_PORT = 18795

# ── Shared state, written by the linker thread, read by the HTTP handlers ────
_state: dict = {"status": "starting", "account": "", "message": "", "uri": "", "qr": ""}
_lock = threading.Lock()
_done = threading.Event()


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _get() -> dict:
    with _lock:
        return dict(_state)


def _qr_data_uri(uri: str) -> str:
    """PNG data URI for the link URI. Empty string when qrcode isn't installed —
    the page then shows the raw URI, which is still usable."""
    try:
        import qrcode  # noqa: WPS433
    except ImportError:
        return ""
    try:
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.resize((512, 512)).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def _signal_cli() -> str:
    return os.environ.get("SIGNAL_CLI_BIN") or shutil.which("signal-cli") or "signal-cli"


def _linker(device_name: str) -> None:
    """Run `signal-cli link`, publish the URI, and restart it when it expires."""
    binary = _signal_cli()
    while not _done.is_set():
        try:
            proc = subprocess.Popen(
                [binary, "link", "-n", device_name],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except FileNotFoundError:
            _set(status="error", message=f"{binary} not found — run admin/deploy.sh first")
            _done.set()
            return

        uri = ""
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            del tail[:-10]
            if not uri and line.startswith("sgnl://"):
                uri = line
                _set(status="qr", uri=uri, qr=_qr_data_uri(uri), message="")
            # Printed once the phone has accepted the link.
            m = re.search(r"(\+\d{6,})", line)
            if m and "Associated" in line:
                _set(status="linked", account=m.group(1), message=line)

        proc.wait()
        out = "\n".join(tail)

        if _get()["status"] == "linked" or proc.returncode == 0:
            if _get()["status"] != "linked":
                acct = ""
                try:
                    r = subprocess.run([binary, "listAccounts"], capture_output=True,
                                       text=True, timeout=15)
                    m = re.search(r"(\+\d{6,})", r.stdout or "")
                    acct = m.group(1) if m else ""
                except Exception:
                    pass
                _set(status="linked", account=acct)
            # Give the page ~3s to render success before the server stops.
            time.sleep(3)
            _done.set()
            return

        if "timed out" in out.lower():
            # Expected: the code expires. Mint a new one instead of making the
            # user restart the whole flow.
            _set(status="starting", uri="", qr="", message="code expired — refreshing")
            continue

        _set(status="error", message=(out or "signal-cli link failed").strip()[-400:])
        _done.set()
        return


PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link Signal · aaka</title>
<style>
  :root{--teal:#00B4A2;--ink:#1E2030;--muted:#556170;--bg:#FFF5F0;--sand:#F0DDD4}
  *{box-sizing:border-box}body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    background:var(--bg);color:var(--ink);display:flex;min-height:100vh;align-items:center;justify-content:center}
  .card{background:#fff;border-radius:20px;box-shadow:0 8px 40px rgba(15,20,25,.10);padding:32px;max-width:440px;width:92%;text-align:center}
  .brand{font-weight:800;font-size:22px;letter-spacing:-.5px}.brand b{color:var(--teal)}
  h1{font-size:19px;margin:.6em 0 .2em}p.sub{color:var(--muted);margin:.2em 0 1.2em;font-size:14px}
  .qr{width:280px;height:280px;margin:8px auto;border-radius:14px;background:#fff;display:flex;align-items:center;justify-content:center;border:1px solid #eef1f3}
  .qr img{width:264px;height:264px;image-rendering:pixelated}
  .steps{text-align:left;font-size:13.5px;color:var(--muted);margin:14px 4px 0;padding-left:18px}
  .steps li{margin:3px 0}
  .spin{width:34px;height:34px;border:3px solid #e3e8eb;border-top-color:var(--teal);
    border-radius:50%;animation:s .8s linear infinite;margin:26px auto}@keyframes s{to{transform:rotate(360deg)}}
  .ok{color:var(--teal);font-size:44px}
  .warn{color:#c2410c;font-size:13px;background:#fff7ed;border-radius:10px;padding:12px;margin-top:8px;text-align:left;white-space:pre-wrap}
  .note{font-size:12.5px;color:var(--muted);background:#f7f9fa;border-radius:10px;padding:10px 12px;margin-top:14px;text-align:left}
  .uri{font:11px/1.4 ui-monospace,Menlo,monospace;word-break:break-all;color:var(--muted);margin-top:10px}
  .pill{display:inline-block;font-size:12px;color:var(--muted);margin-top:14px}
</style></head><body>
<div class="card">
  <div class="brand">&amp; aaka<b>.</b></div>
  <div id="view"><div class="spin"></div><p class="sub">Starting signal-cli…</p></div>
  <span class="pill" id="pill"></span>
</div>
<script>
const view=document.getElementById('view'),pill=document.getElementById('pill');
const steps='<ol class="steps">'+
  '<li>Open <b>Signal</b> on your phone</li>'+
  '<li>Tap <b>Settings \\u2192 Linked Devices</b></li>'+
  '<li>Tap <b>+</b> (or <b>Link New Device</b>)</li>'+
  '<li>Point the camera at this code</li></ol>'+
  '<div class="note"><b>This links aaka to your own Signal account.</b> '+
  'aaka will see your conversations and reply as you, so it stays silent on '+
  'anything that is not an explicit command.</div>';
async function tick(){
  try{
    const s=await (await fetch('/status')).json();
    pill.textContent='status: '+s.status;
    if(s.status==='linked'){
      view.innerHTML='<div class="ok">\\u2713</div><h1>Signal linked</h1>'+
        '<p class="sub">'+(s.account||'')+'</p>'+
        '<p class="sub">You can close this page. Run <b>bash admin/deploy.sh</b> next.</p>';
      return;}
    if(s.status==='error'){
      view.innerHTML='<h1>Linking failed</h1><div class="warn">'+
        (s.message||'unknown error')+'</div>';return;}
    if(s.status==='qr'){
      const q=await (await fetch('/qr')).json();
      view.innerHTML='<h1>Scan to link Signal</h1>'+
        '<p class="sub">The code refreshes on its own \\u2014 no rush</p>'+
        (q.qr?('<div class="qr"><img src="'+q.qr+'"></div>'):
              ('<div class="warn">qrcode not installed \\u2014 paste this into any QR generator</div>'+
               '<div class="uri">'+(q.uri||'')+'</div>'))+steps;
      return;}
    view.innerHTML='<div class="spin"></div><p class="sub">'+
      (s.message||'Requesting a link code\\u2026')+'</p>';
  }catch(e){pill.textContent='pairing server offline';}
}
tick();setInterval(tick,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/status"):
            s = _get()
            self._send(200, json.dumps({
                "status": s["status"], "account": s["account"], "message": s["message"],
            }).encode(), "application/json")
        elif self.path.startswith("/qr"):
            s = _get()
            self._send(200, json.dumps({"qr": s["qr"], "uri": s["uri"]}).encode(),
                       "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def log_message(self, *_a):  # keep the console clean
        return


def main() -> int:
    ap = argparse.ArgumentParser(description="Signal pairing page")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SIGNAL_PAIR_PORT", DEFAULT_PORT)))
    ap.add_argument("--name", default="aaka", help="device name shown in Signal")
    ap.add_argument("--no-open", action="store_true", help="don't launch a browser")
    args = ap.parse_args()

    # Loopback only: the link URI is a credential — anyone who scans it gets a
    # device on the account.
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_linker, args=(args.name,), daemon=True).start()

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Signal pairing page: {url}")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("Scan the code from Signal → Settings → Linked Devices. Ctrl-C to abort.")

    try:
        while not _done.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\naborted")
        return 130
    finally:
        server.shutdown()

    s = _get()
    if s["status"] == "linked":
        print(f"✓ linked{(' as ' + s['account']) if s['account'] else ''}")
        return 0
    print(f"✗ {s['message'] or 'linking failed'}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
