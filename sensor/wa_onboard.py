#!/usr/bin/env python3
"""
sensor/wa_onboard.py — invite-code onboarding for WhatsApp (and Telegram).

Flow:
  1. Admin: `/invite <name>` → create_invite() mints a one-time code tied to an
     existing member and returns a wa.me deep link with the code pre-filled.
  2. Admin shares the link; the invitee taps it → WhatsApp opens pre-filled →
     they send.
  3. The invitee is an unknown sender, so the router calls try_signup(); a valid
     unexpired code binds their handle (@lid or +E.164) into the dynamic
     allowlist (data/wa_allowlist.json, read by aaka_config.member_by_sender) and
     returns a warm welcome. No aaka.yaml editing; the LID opacity stops mattering
     because we bind whatever handle actually arrives.

Stores (config dir, never git):
  data/wa_invites.json    {code: {member_id, name, expires_ts, consumed}}
  data/wa_allowlist.json  {handle_lower: member_id}   (shared with aaka_config)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.request
from pathlib import Path

import aaka_config

_CODE_ALPHABET = "ACDEFGHJKMNPQRSTUVWXYZ2345679"  # no ambiguous 0/O/1/I/L/B/8
_CODE_LEN = 4
_INVITE_TTL_S = 7 * 24 * 3600  # 7 days


def _cfg_dir() -> Path:
    return Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))


def _invites_path() -> Path:
    return _cfg_dir() / "data" / "wa_invites.json"


def _load_invites() -> dict:
    p = _invites_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def create_invite(member_id: str, name: str) -> str:
    """Mint a one-time code for `member_id`. Returns the code."""
    invites = _load_invites()
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    while code in invites:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    invites[code] = {
        "member_id": member_id,
        "name": name,
        "expires_ts": int(time.time()) + _INVITE_TTL_S,
        "consumed": False,
    }
    _save_json(_invites_path(), invites)
    return code


def bind(handle: str, member_id: str) -> None:
    """Add handle → member_id to the dynamic allowlist (data/wa_allowlist.json)."""
    p = _cfg_dir() / "data" / "wa_allowlist.json"
    al = {}
    try:
        if p.exists():
            al = json.loads(p.read_text())
    except Exception:
        al = {}
    al[str(handle).strip().lower()] = member_id
    _save_json(p, al)


def _find_code(text: str) -> str | None:
    """Extract a plausible code token from free text (e.g. 'Hi rosi (A7X2)')."""
    for tok in re.findall(r"[A-Za-z0-9]{%d}" % _CODE_LEN, text or ""):
        if tok.upper() in _load_invites() or all(c in _CODE_ALPHABET for c in tok.upper()):
            up = tok.upper()
            if up in _load_invites():
                return up
    return None


def try_signup(handle: str, sender_name: str, text: str) -> str | None:
    """If `text` carries a valid unexpired invite code, bind `handle` to the
    member and return a welcome message. Otherwise None (caller falls back)."""
    code = _find_code(text)
    if not code:
        return None
    invites = _load_invites()
    inv = invites.get(code)
    if not inv or inv.get("consumed"):
        return None
    if int(inv.get("expires_ts", 0)) < int(time.time()):
        return None

    member_id = inv["member_id"]
    bind(handle, member_id)
    inv["consumed"] = True
    inv["bound_handle"] = handle
    inv["bound_ts"] = int(time.time())
    _save_json(_invites_path(), invites)

    name = inv.get("name") or sender_name or ""
    bot = _bot_name()
    hi = f"🎉 Hi {name}, you're all set!" if name else "🎉 You're all set!"
    return (
        f"{hi} I'm {bot}, your family assistant.\n\n"
        f"Try:\n"
        f"• `t buy milk` — add a task\n"
        f"• `/today` — today's plan\n"
        f"• `/menu` — everything I can do\n\n"
        f"Just message me any time."
    )


def _bot_name() -> str:
    try:
        return (aaka_config._load().get("system", {}) or {}).get("bot_name", "aaka")
    except Exception:
        return "aaka"


def aaka_wa_number() -> str | None:
    """aaka's own WhatsApp number in bare digits, from the sidecar /status jid.
    Returns None if the sidecar isn't reachable / not connected."""
    port = os.environ.get("WA_SIDECAR_PORT", "18792")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as r:
            data = json.loads(r.read() or b"{}")
        jid = data.get("jid") or ""
        digits = re.sub(r"[^0-9]", "", jid.split("@")[0].split(":")[0])
        return digits or None
    except Exception:
        return None


def invite_link(code: str, name: str = "") -> tuple[str, str]:
    """Return (wa_me_url, plain_instructions). Uses aaka's own number for the
    deep link; falls back to instructions if the number can't be resolved."""
    bot = _bot_name()
    who = f", it's {name}" if name else ""
    text = f"Hi {bot}{who} ({code})"
    num = aaka_wa_number()
    if num:
        import urllib.parse
        url = f"https://wa.me/{num}?text={urllib.parse.quote(text)}"
        return url, text
    return "", text
