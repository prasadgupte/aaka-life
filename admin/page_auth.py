#!/usr/bin/env python3
"""page_auth.py — manage the secret for aaka's portable web-page guard (webauth.py).

    python3 admin/page_auth.py init      # generate a secret (if none) → tokens/page_auth.json
    python3 admin/page_auth.py show      # print the sign-in key + a ?k= hint (secret is sensitive)
    python3 admin/page_auth.py rotate    # new secret (invalidates existing sign-ins)
    python3 admin/page_auth.py status    # is a secret configured?

Once a secret exists, aaka page servers (e.g. taskboard) may bind publicly and will
gate every request. Sign in by opening the page once with `?k=<secret>`.
"""
import json
import os
import secrets
import stat
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import webauth  # noqa: E402


def _path() -> Path:
    p = webauth._conf_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write(conf: dict) -> None:
    p = _path()
    p.write_text(json.dumps(conf, indent=2))
    p.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600


def cmd_init(force=False) -> None:
    p = _path()
    if p.exists() and not force and webauth.page_secret():
        print(f"already configured: {p} (use 'rotate' to replace)")
        return
    _write({"secret": secrets.token_urlsafe(24), "session_key": secrets.token_urlsafe(32)})
    print(f"✅ page secret written to {p} (mode 600). Run 'show' for the sign-in key.")


def cmd_show() -> None:
    s = webauth.page_secret()
    if not s:
        print("no secret configured — run: python3 admin/page_auth.py init")
        return
    print("sign-in key (keep private):", s)
    print("sign in by opening the page once with:  ?k=" + s)


def cmd_status() -> None:
    print("page auth:", "configured ✅" if webauth.page_secret() else "not set (localhost-only)")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "init":
        cmd_init()
    elif cmd == "rotate":
        cmd_init(force=True)
    elif cmd == "show":
        cmd_show()
    else:
        cmd_status()


if __name__ == "__main__":
    main()
