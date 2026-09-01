#!/usr/bin/env python3
"""
sensor/test_wa_onboard.py — invite-code onboarding flow (no network).

Uses a throwaway AAKA_CONFIG_DIR with a minimal aaka.yaml, then exercises:
mint → sign up (bind handle) → recognized via dynamic allowlist → one-time.

Run: /Users/Shared/aaka-repo/venv/bin/python3 sensor/test_wa_onboard.py
"""
import os
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
    check("unknown member rejected", "No member" in _handle_invite("/invite Bob", "+491700000000"))

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

    print()
    if _FAIL:
        print(f"FAILED: {len(_FAIL)} check(s)")
        return 1
    print("all wa_onboard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
