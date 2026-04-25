#!/usr/bin/env python3
"""
Re-authorize Google OAuth for Aaka accounts.

Usage:
  ./reauth.py           # interactive menu
  ./reauth.py alice     # direct (alice's calendar token — member ID from aaka.yaml)
  ./reauth.py bob       # direct (bob's gmail + tasks + calendar token)

Scopes are read from config/aaka.yaml auth blocks.
"""

import os, sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or Path(__file__).resolve().parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config

# credentials.json resolution order:
#   1. $AAKA_CONFIG_DIR/tokens/credentials.json  — user's own GCP project (takes priority)
#   2. <repo>/config/credentials.json             — bundled Aaka community app (no GCP account needed)
_CREDS_USER    = aaka_config.TOKENS_DIR / "credentials.json"
_CREDS_BUNDLED = BASE / "config" / "credentials.json"

if _CREDS_USER.exists():
    CREDS = _CREDS_USER
elif _CREDS_BUNDLED.exists():
    CREDS = _CREDS_BUNDLED
    print("ℹ️  Using bundled Aaka community OAuth app.")
    print("   Your token stays on your machine — no data touches our servers.")
    print(f"   (To use your own GCP project instead: place credentials.json at {_CREDS_USER})\n")
else:
    print("❌  credentials.json not found.\n")
    print("Option A — Use the Aaka community app (no GCP account needed):")
    print("   The bundled credentials file is missing. This may mean you need to")
    print("   pull the latest repo, or the community app hasn't been published yet.")
    print()
    print("Option B — Use your own GCP project:")
    print("   1. console.cloud.google.com → create/select a project")
    print("   2. APIs & Services → Library → enable: Google Calendar API, Google People API")
    print("   3. Credentials → Create → OAuth 2.0 Client ID → Desktop app → Download JSON")
    print(f"   4. Save as: {_CREDS_USER}")
    print("   5. Re-run this script.")
    raise SystemExit(1)

# ── --profile vps: scoped token for sensor (calendar.events + tasks only) ─────
VPS_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
]

if len(sys.argv) >= 3 and sys.argv[1] == "--profile" and sys.argv[2] == "vps":
    token_path = aaka_config.TOKENS_DIR / "token_vps.json"

    # Find which member owns tasks (they share the VPS token's Google account)
    _tasks_owner = next(
        (m for m in aaka_config.auth_members()
         if any("tasks" in s for s in m.get("auth", {}).get("scopes", []))),
        None,
    )
    _owner_hint = f"  → log in as {_tasks_owner['email']}" if _tasks_owner and _tasks_owner.get("email") else ""

    print(f"\n🔑 VPS scoped token")
    print(f"   Token file : {token_path}")
    print(f"   Scopes     : {VPS_SCOPES}")
    if _tasks_owner:
        print(f"   Account    : {_tasks_owner.get('email', _tasks_owner['name'])}{_owner_hint and ''}")
    print(f"\n⚠️  Log in as {_tasks_owner['email'] if _tasks_owner else 'the Google account that owns calendar/tasks'}.\n")
    if token_path.exists():
        token_path.unlink()
        print(f"🗑  Removed old {token_path.name}")
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = InstalledAppFlow.from_client_secrets_file(str(CREDS), VPS_SCOPES).run_local_server(port=0)
    token_path.write_text(creds.to_json())
    print(f"\n✅ VPS token saved: {token_path}")
    print(f"   Scopes granted: {sorted(creds.scopes)}")
    import os as _os
    _vps = _os.environ.get("AAKA_VPS_HOST", "aaka-away")
    print(f"\nNext steps:")
    print(f"  scp {token_path} {_vps}:/opt/aaka-config/tokens/token_vps.json")
    print(f"  ssh {_vps} 'chmod 600 /opt/aaka-config/tokens/token_vps.json'")
    raise SystemExit(0)

auth_members = aaka_config.auth_members()
if not auth_members:
    print("❌ No members with auth blocks found in aaka.yaml")
    raise SystemExit(1)

# Resolve target member
member_id = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""

if not member_id:
    print("Which account do you want to re-authorize?\n")
    for i, m in enumerate(auth_members, 1):
        label = m["name"]
        email = m.get("email", "")
        print(f"  ({i}) {label}  <{email}>")
    print()
    try:
        choice = input("Enter number: ").strip()
        idx = int(choice) - 1
        if idx < 0 or idx >= len(auth_members):
            raise ValueError
        member = auth_members[idx]
    except (ValueError, EOFError):
        print("❌ Invalid selection.")
        raise SystemExit(1)
else:
    member = next((m for m in auth_members if m["id"] == member_id), None)
    if not member:
        valid = ", ".join(m["id"] for m in auth_members)
        print(f"❌ Unknown member id '{member_id}'. Valid: {valid}")
        raise SystemExit(1)

auth = aaka_config.auth_for(member["id"])
token_path = auth["token_file"]
scopes     = auth["scopes"]

print(f"\n🔑 Re-authorizing: {member['name']}  <{member.get('email', '')}>")
print(f"   Token file : {token_path}")
print(f"   Scopes     : {len(scopes)} scope(s)")
for s in scopes:
    print(f"               {s}")
print(f"\n⚠️  Please log in as {member.get('email', '')} in the browser window.\n")

if token_path.exists():
    token_path.unlink()
    print(f"🗑  Removed old {token_path.name}")

from google_auth_oauthlib.flow import InstalledAppFlow

flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS), scopes)
creds = flow.run_local_server(port=0)
token_path.write_text(creds.to_json())

print(f"\n✅ New token saved: {token_path}")
print(f"   Scopes granted: {sorted(creds.scopes)}")
print(f"\nDone. {member['name']}'s services will use this token now.")

# Auto-push aakash's token to VPS — needed for sensor/scheduled_sender.py (gmail.send)
if member["id"] == "aakash":
    import subprocess as _sp
    _vps = os.environ.get("AAKA_VPS_HOST", "aaka-away")
    print(f"\n🚀 Pushing token_aakash.json to {_vps}…")
    r = _sp.run(
        ["scp", str(token_path), f"{_vps}:/opt/aaka-config/tokens/token_aakash.json"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        _sp.run(["ssh", _vps, "chmod 600 /opt/aaka-config/tokens/token_aakash.json"])
        print(f"   ✓ token_aakash.json deployed to {_vps}")
    else:
        print(f"   ✗ scp failed: {r.stderr.strip()}")
        print(f"     Manual: scp {token_path} {_vps}:/opt/aaka-config/tokens/token_aakash.json")
