#!/usr/bin/env python3
"""vps_2fa.py — executor-side control for the VPS's unified 2FA gate.

The VPS already runs one TOTP auth service (`/opt/auth/auth.py` on :8001) that
Caddy uses via `forward_auth` to gate routes (playlist, static pages, …). This is
**VPS-wide, not aaka-specific** — every protected route shares ONE secret / ONE
authenticator entry. This tool manages that shared gate from the Mac:

    python3 admin/vps_2fa.py qr                 # show the QR to add it to an authenticator app
    python3 admin/vps_2fa.py verify 123456      # confirm a code works (the "enter code to be sure" step)
    python3 admin/vps_2fa.py rotate             # new secret → relay to VPS → restart auth → new QR (destructive)
    python3 admin/vps_2fa.py protect /board localhost:8002   # gate an aaka away route behind the same 2FA
    python3 admin/vps_2fa.py status             # which routes are protected + auth service health

Design: the secret is the source of truth on the VPS (`/opt/auth/auth_config.json`,
mode 600). We never print the raw secret to chat — it goes into the QR only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import subprocess
import sys
import time
import urllib.parse

import os

VPS = os.environ.get("AAKA_VPS_HOST", "aaka-away")  # SSH alias; override per host
AUTH_CONF = "/opt/auth/auth_config.json"
CADDYFILE = "/etc/caddy/Caddyfile"
ISSUER = "Aaka"


def _ssh(cmd: str, check: bool = True) -> str:
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=12", "-o", "BatchMode=yes", VPS, cmd],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"ssh failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _load_conf() -> dict:
    return json.loads(_ssh(f"cat {AUTH_CONF}"))


def _totp_now(secret_b32: str) -> str:
    key = base64.b32decode(secret_b32.upper().replace(" ", ""))
    counter = struct.pack(">Q", int(time.time()) // 30)
    h = hmac.new(key, counter, hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _valid_codes(secret_b32: str) -> set:
    key = base64.b32decode(secret_b32.upper().replace(" ", ""))
    step = int(time.time()) // 30
    out = set()
    for i in (-1, 0, 1):
        counter = struct.pack(">Q", step + i)
        h = hmac.new(key, counter, hashlib.sha1).digest()
        off = h[-1] & 0x0F
        out.add(f"{(struct.unpack('>I', h[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}")
    return out


def _otpauth_uri(secret_b32: str) -> str:
    label = urllib.parse.quote(f"{ISSUER}:{VPS}")
    return f"otpauth://totp/{label}?secret={secret_b32}&issuer={urllib.parse.quote(ISSUER)}&period=30&digits=6"


def cmd_qr() -> None:
    secret = _load_conf()["totp_secret"]
    uri = _otpauth_uri(secret)
    out = "/tmp/aaka_vps_2fa_qr.png"
    try:
        import qrcode
        qrcode.make(uri).save(out)
        print(f"QR written to {out}")
    except Exception as e:
        print(f"(qrcode render failed: {e})")
        out = ""
    # ASCII fallback so it's usable even without the image
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(uri)
        q.print_ascii(invert=True)
    except Exception:
        pass
    print("\nScan into any authenticator app (Google Authenticator, Authy, 1Password).")
    print("This is the SAME entry that already guards the playlist — one code for everything.")
    print(f"otpauth (manual): {uri[:40]}… (secret hidden)")
    print(f"PNG: {out}" if out else "")


def cmd_verify(code: str) -> None:
    secret = _load_conf()["totp_secret"]
    ok = code.strip() in _valid_codes(secret)
    print("✅ Code valid — your authenticator is set up correctly." if ok
          else "❌ Code rejected — check the authenticator entry (or clock skew).")
    sys.exit(0 if ok else 1)


def cmd_rotate() -> None:
    new_secret = base64.b32encode(hashlib.sha256(str(time.time()).encode()).digest()[:20]).decode().rstrip("=")
    conf = _load_conf()
    conf["totp_secret"] = new_secret
    payload = json.dumps(conf)
    print("⚠️ This invalidates the current code on ALL protected routes (playlist too).")
    if input("Type 'rotate' to confirm: ").strip() != "rotate":
        print("aborted."); return
    _ssh(f"cat > {AUTH_CONF} <<'EOF'\n{payload}\nEOF\nchmod 600 {AUTH_CONF} && (systemctl restart auth || true)")
    print("new secret relayed + auth restarted.\n")
    cmd_qr()


def cmd_protect(route: str, upstream: str) -> None:
    """Emit (and offer to apply) a forward_auth-gated Caddy handle for an aaka route."""
    route = "/" + route.strip("/")
    if ":" in upstream:  # reverse proxy to a local port
        body = (f'            uri strip_prefix {route}\n'
                f'            reverse_proxy {upstream} {{\n'
                f'                header_up Host {{host}}\n'
                f'                header_up X-Real-IP {{remote_host}}\n'
                f'            }}')
    else:  # static dir
        body = f'            uri strip_prefix {route}\n            root * {upstream}\n            file_server'
    block = (f'    handle {route}/* {{\n'
             f'        route {{\n'
             f'            forward_auth localhost:8001 {{\n'
             f'                uri /auth/check\n'
             f'                copy_headers Cookie\n'
             f'            }}\n'
             f'{body}\n'
             f'        }}\n'
             f'    }}')
    print("Add this inside the your-vps-host.example { … } block in the Caddyfile:\n")
    print(block)
    print("\nThen: caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy")
    print("(Run `admin/vps_2fa.py apply` once you've reviewed — not auto-applied here to protect the live front door.)")


def cmd_status() -> None:
    print("auth service:", _ssh("systemctl is-active auth", check=False).strip() or "unknown")
    protected = _ssh(f"grep -B2 'forward_auth' {CADDYFILE} | grep 'handle ' || true", check=False)
    print("protected routes:\n" + (protected.strip() or "  (none found)"))


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__); return
    cmd, rest = args[0], args[1:]
    if cmd == "qr":
        cmd_qr()
    elif cmd == "verify" and rest:
        cmd_verify(rest[0])
    elif cmd == "rotate":
        cmd_rotate()
    elif cmd == "protect" and len(rest) == 2:
        cmd_protect(rest[0], rest[1])
    elif cmd == "status":
        cmd_status()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
