"""webauth.py — portable, app-level auth guard for aaka web pages.

Secure-by-default *exposure*: an aaka page server refuses to bind to a non-loopback
interface unless a secret is configured, and when a secret IS set it gates every
request behind it (magic-link `?k=<secret>` → signed cookie). This protects aaka's
own pages even when a reverse proxy is bypassed or absent — **no proxy dependency**.

The three states (independent of any proxy you may also run):

    localhost bind, no secret  →  open        (dev / behind a trusting proxy)  [default]
    public bind,    no secret  →  REFUSED      (can't accidentally expose unauthed)
    any bind,       secret set →  gated        (cookie or ?k=<secret>)

For a Caddy-gated deployment you simply don't set the app secret: the page binds
127.0.0.1, the proxy authenticates, and this guard stays dormant (no double-auth).
The guard is what makes exposure safe for installers who DON'T have a proxy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}
COOKIE = "aaka_page"
_TTL = 30 * 24 * 3600  # 30 days


def _conf_path() -> Path:
    root = os.environ.get("AAKA_CONFIG_DIR", "/Users/Shared/aaka-repo-config")
    return Path(root) / "tokens" / "page_auth.json"


def _load() -> dict:
    # Env override wins — handy for tests and ephemeral runs.
    s = os.environ.get("AAKA_PAGE_SECRET", "").strip()
    if s:
        return {"secret": s, "session_key": os.environ.get("AAKA_PAGE_SESSION_KEY", s)}
    p = _conf_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def page_secret() -> str:
    return (_load().get("secret") or "").strip()


def _session_key() -> bytes:
    d = _load()
    return (d.get("session_key") or d.get("secret") or "").encode()


def is_public_host(host: str) -> bool:
    return (host or "").strip() not in _LOOPBACK


def assert_safe_bind(host: str) -> None:
    """Refuse a public bind with no secret configured — the anti-footgun.

    Call this before starting the server. Raises SystemExit with guidance."""
    if is_public_host(host) and not page_secret():
        raise SystemExit(
            f"\n❌ Refusing to bind {host!r} with no page auth configured.\n"
            f"   Exposing an aaka page unauthenticated is unsafe (a proxy in front is\n"
            f"   not enough — a direct hit to the port bypasses it). Choose one:\n"
            f"     • bind 127.0.0.1 (default) and put your own authenticating proxy in front, or\n"
            f"     • run:  python3 admin/page_auth.py init   (generates a secret, then this is allowed)\n"
        )


def _sign(exp: int) -> str:
    mac = hmac.new(_session_key(), str(exp).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{mac}"


def _valid_cookie(val: str) -> bool:
    try:
        exp_s, mac = (val or "").split(".", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_sign(exp).split(".", 1)[1], mac)


def make_cookie() -> str:
    return _sign(int(time.time()) + _TTL)


def install_guard(app, open_paths=("/health",)):
    """Add the request gate to a FastAPI/Starlette app. No-op when no secret is
    set (localhost mode). Idempotent-safe to call once at import time."""
    from starlette.responses import PlainTextResponse, RedirectResponse

    @app.middleware("http")
    async def _guard(request, call_next):
        secret = page_secret()
        if not secret:  # localhost / proxy mode — nothing to enforce
            return await call_next(request)
        if request.url.path in open_paths:
            return await call_next(request)
        # Magic-link entry: ?k=<secret> once → set a signed cookie, strip the param.
        k = request.query_params.get("k", "")
        if k and hmac.compare_digest(k, secret):
            resp = RedirectResponse(url=request.url.path, status_code=303)
            resp.set_cookie(COOKIE, make_cookie(), httponly=True, samesite="lax", max_age=_TTL)
            return resp
        if _valid_cookie(request.cookies.get(COOKIE, "")):
            return await call_next(request)
        return PlainTextResponse(
            "🔒 aaka: authentication required.\n"
            "Open this page with ?k=<your key> once to sign in.",
            status_code=401,
        )

    return app
