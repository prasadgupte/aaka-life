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


# On-brand, self-contained (no external assets — a guard page must render even if
# everything else is gated). Uses the canonical aaka palette (see
# executor/webui/brand/tokens.css) + the & logo mark. {err} placeholder is filled
# via str.replace (so CSS braces need no escaping).
_LOGIN_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>aaka · sign in</title>
<style>
:root{--teal:#00B4A2;--ink:#1E2030;--slate:#556170;--bg:#FFF5F0;--surface:#fff;--sand:#F0DDD4;--berry:#e0456b}
*{box-sizing:border-box}
body{font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink);
display:grid;place-items:center;min-height:100vh;margin:0}
.card{background:var(--surface);border:1px solid var(--sand);border-radius:20px;
box-shadow:0 10px 40px rgba(30,32,48,.10);padding:2.4rem 2rem;width:min(92vw,22rem);text-align:center}
.logo{width:56px;height:56px;border-radius:16px;background:var(--teal);display:grid;place-items:center;
margin:0 auto .9rem;box-shadow:0 4px 14px rgba(0,180,162,.35)}
.logo svg{width:40px;height:40px}
.wm{font-weight:800;font-size:1.5rem;letter-spacing:-.5px;margin:0}
p{color:var(--slate);font-size:.9rem;margin:.35rem 0 1.3rem}
input{width:100%;padding:.7rem .8rem;border:1px solid var(--sand);border-radius:11px;background:#fff;
color:var(--ink);font-size:1rem}
input:focus{outline:none;border-color:var(--teal);box-shadow:0 0 0 3px rgba(0,180,162,.15)}
button{width:100%;margin-top:.9rem;padding:.72rem;border:0;border-radius:11px;background:var(--teal);
color:var(--ink);font-weight:700;font-size:1rem;cursor:pointer}button:hover{filter:brightness(1.04)}
.e{color:var(--berry);font-size:.85rem;margin-top:.8rem}
</style></head><body><form method=get class=card>
<div class=logo><svg viewBox="0 0 100 100"><text x=50 y=80 font-size=86 font-family="Georgia,serif"
font-weight=bold text-anchor=middle fill=white>&amp;</text></svg></div>
<h1 class=wm>aaka</h1><p>Enter your access key to continue.</p>
<input name=k type=password autofocus placeholder="access key">
<button>Sign in</button>{err}</form></body></html>"""


def install_guard(app, open_paths=("/health",)):
    """Add the request gate to a FastAPI/Starlette app. No-op when no secret is
    set (localhost mode). Idempotent-safe to call once at import time.

    UX: unauthenticated requests get a small sign-in page; a correct key sets a
    30-day signed cookie and serves the request (no redirect — subpath-safe under
    a strip_prefix proxy). `?k=<key>` also works as a one-tap magic link."""
    from starlette.responses import HTMLResponse

    @app.middleware("http")
    async def _guard(request, call_next):
        secret = page_secret()
        if not secret:  # localhost / proxy mode — nothing to enforce
            return await call_next(request)
        if request.url.path in open_paths:
            return await call_next(request)
        k = request.query_params.get("k", "")
        if k:
            if hmac.compare_digest(k, secret):
                resp = await call_next(request)  # serve + set cookie (no redirect)
                resp.set_cookie(COOKIE, make_cookie(), httponly=True, samesite="lax", max_age=_TTL)
                return resp
            return HTMLResponse(_LOGIN_HTML.replace("{err}", "<div class=e>Wrong key — try again.</div>"),
                                status_code=401)
        if _valid_cookie(request.cookies.get(COOKIE, "")):
            return await call_next(request)
        return HTMLResponse(_LOGIN_HTML.replace("{err}", ""), status_code=401)

    return app
