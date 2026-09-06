#!/usr/bin/env python3
"""
sensor/test_wa_onboard.py — invite-code onboarding flow (no network).

Uses a throwaway AAKA_CONFIG_DIR with a minimal aaka.yaml, then exercises:
mint → sign up (bind handle) → recognized via dynamic allowlist → one-time.

Run: /Users/Shared/aaka-repo/venv/bin/python3 sensor/test_wa_onboard.py
"""
import os
import time
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="aaka-wa-onboard-"))
(_TMP / "config").mkdir(parents=True, exist_ok=True)
(_TMP / "data").mkdir(parents=True, exist_ok=True)
(_TMP / "config" / "aaka.yaml").write_text(
    "system: {timezone: 'Europe/Berlin', bot_name: 'Rosi'}\n"
    "members:\n"
    "  - {id: 'alex', name: 'Alex', admin: true, whatsapp: '+491700000000'}\n"
    "  - {id: 'sam', name: 'Sam'}\n"
    "calendar: {default_id: primary}\n"
)
os.environ["AAKA_CONFIG_DIR"] = str(_TMP)

import aaka_config as c            # noqa: E402
from sensor import wa_onboard      # noqa: E402
from sensor.router_sensor import _handle_invite  # noqa: E402
from sensor.intent_registry import match_intent  # noqa: E402

_FAIL = []


def check(desc, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {desc}")
    if not cond:
        _FAIL.append(desc)


def main():
    check("/invite matches the invite intent", match_intent("/invite sam") == "invite")

    # admin gate
    check("non-admin blocked", "Only an admin" in _handle_invite("/invite Sam", "99999@lid"))

    # unknown name → created on the fly (no aaka.yaml edit)
    before = {m.get("id") for m in c.members()}
    out = _handle_invite("/invite Robin", "+491700000000")
    check("unknown name creates a member", "Added" in out and "Invite" in out)
    check("new member appears in members()", "robin" in {m.get("id") for m in c.members()})
    check("existing members untouched", before <= {m.get("id") for m in c.members()})

    # mint + sign up
    code = wa_onboard.create_invite("sam", "Sam")
    handle = "93621781041232@lid"
    check("unknown before signup", c.member_by_sender(handle) is None)
    welcome = wa_onboard.try_signup(handle, "Sam WA", f"Hi Rosi, it's Sam ({code})")
    check("signup returns a welcome", bool(welcome) and "all set" in (welcome or ""))
    m = c.member_by_sender(handle)
    check("recognized after signup (dynamic allowlist)", bool(m) and m.get("id") == "sam")

    # one-time
    check("code cannot be reused", wa_onboard.try_signup("other@lid", "x", f"code {code}") is None)
    check("garbage text is not a signup", wa_onboard.try_signup("z@lid", "z", "hello there") is None)

    # ── SEC-4: invite codes must resist online guessing ──────────────────────
    check("code length is 8", len(code) == 8 and wa_onboard._CODE_LEN == 8)
    check("invite TTL is 24h", wa_onboard._INVITE_TTL_S == 24 * 3600)
    inv = wa_onboard._load_invites()[code]
    check("minted expiry honours the 24h TTL",
          inv["expires_ts"] - int(time.time()) <= 24 * 3600 + 5)

    # Only ONE candidate token is tested per message, even when the text is
    # stuffed with plausible-looking tokens.
    stuffed = " ".join("AAAAAAAA BBBBBBBB CCCCCCCC DDDDDDDD".split())
    check("only the first plausible token is tested",
          wa_onboard._find_code(stuffed) == "AAAAAAAA")
    check("tokens with out-of-alphabet chars are skipped",
          wa_onboard._find_code("00000000 AAAAAAAA") == "AAAAAAAA")

    # Failed-attempt throttle: 5 bad codes in the window, then silence.
    guesser = "bruteforce@lid"
    live = wa_onboard.create_invite("sam", "Sam")
    for i in range(wa_onboard._MAX_FAILS):
        wa_onboard.try_signup(guesser, "g", "QQQQQQQQ")
    check("throttle engages after 5 failed codes", wa_onboard._throttled(guesser))
    check("a VALID code is ignored once throttled",
          wa_onboard.try_signup(guesser, "g", f"code {live}") is None)
    check("attempts file written",
          (Path(os.environ["AAKA_CONFIG_DIR"]) / "data" / "wa_invite_attempts.json").exists())
    # A different sender is unaffected.
    check("throttle is per-sender",
          wa_onboard.try_signup("clean@lid", "c", f"code {live}") is not None)

    print()
    if _FAIL:
        print(f"FAILED: {len(_FAIL)} check(s)")
        return 1
    print("all wa_onboard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
