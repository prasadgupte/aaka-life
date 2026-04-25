#!/usr/bin/env python3
"""Auth token validation + GCal heartbeat helper."""
import json, sys, time
from pathlib import Path
import os

# When run as a script, Python inserts the script dir (skills/) into sys.path[0].
# This causes skills/calendar/ to shadow Python's stdlib `calendar` module,
# breaking google-auth's requests transport. Remove it before any imports.
_skills_dir = str(Path(__file__).resolve().parent)
if _skills_dir in sys.path:
    sys.path.remove(_skills_dir)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aaka_config
from datetime import timezone

def check_token(member_id: str) -> dict:
    """Validate token file for member. Returns dict with valid, expires_at, scopes, token_file, error."""
    try:
        info = aaka_config.auth_for(member_id)
        if not info:
            return {"valid": False, "error": f"no auth block for {member_id}", "token_file": None}
        token_path = info["token_file"]
        if not token_path.exists():
            return {"valid": False, "error": f"token file missing: {token_path}", "token_file": str(token_path)}
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(token_path))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        expires_at = creds.expiry.replace(tzinfo=timezone.utc).isoformat() if creds.expiry else None
        return {
            "valid": creds.valid,
            "expires_at": expires_at,
            "scopes": list(creds.scopes or []),
            "token_file": str(token_path),
            "error": None,
        }
    except Exception as e:
        return {"valid": False, "error": str(e), "token_file": None}

def heartbeat_calendar(member_id: str) -> dict:
    """Ping GCal API for member. Returns dict with ok, latency_ms, error."""
    try:
        info = aaka_config.auth_for(member_id)
        if not info:
            return {"ok": False, "error": f"no auth block for {member_id}"}
        token_path = info["token_file"]
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(str(token_path))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        t0 = time.time()
        svc.calendarList().list(maxResults=1).execute()
        latency = int((time.time() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "error": None}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": str(e)}

if __name__ == "__main__":
    args = sys.argv[1:]
    member_arg = None
    if "--member" in args:
        idx = args.index("--member")
        if idx + 1 < len(args):
            member_arg = args[idx + 1]
    do_heartbeat = "--heartbeat" in args

    members = aaka_config.auth_members()
    targets = [m for m in members if member_arg is None or m["id"] == member_arg]
    if not targets:
        print(json.dumps({"error": f"no auth members found"}))
        sys.exit(1)

    results = []
    for m in targets:
        mid = m["id"]
        tok = check_token(mid)
        result = {"member": mid, **tok}
        if do_heartbeat and tok["valid"]:
            hb = heartbeat_calendar(mid)
            result.update(hb)
        results.append(result)

    print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    any_invalid = any(not r.get("valid") for r in results)
    sys.exit(1 if any_invalid else 0)
