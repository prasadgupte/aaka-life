#!/usr/bin/env python3
"""
admin/signal_chats.py — list the Signal chats aaka can see, with their ids.

SIGNAL_ALLOWED_CHATS is deny-by-default: aaka speaks only in chats you list.
This prints the ids to put there, and marks which are already allowed — the
alternative is guessing at base64 group ids.

Asks the running daemon over JSON-RPC. It never invokes signal-cli directly:
the CLI blocks while the daemon holds the account lock.

Run:  python3 admin/signal_chats.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _rpc(method: str, params: dict | None = None, timeout: int = 20) -> dict:
    url = os.environ.get("SIGNAL_CLI_URL", "http://127.0.0.1:18794").rstrip("/")
    body = json.dumps({"jsonrpc": "2.0", "id": "chats", "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(f"{url}/api/v1/rpc", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def main() -> int:
    try:
        from sensor.router_sensor import _signal_allowed_chats  # noqa: WPS433
        allowed = _signal_allowed_chats()
    except Exception:
        allowed = set()

    try:
        data = _rpc("listGroups")
    except (urllib.error.URLError, OSError) as exc:
        print(f"signal-cli daemon unreachable: {exc}", file=sys.stderr)
        print("Start it first (launchctl start com.aaka.signalcli, or "
              "systemctl start aaka-signal-cli).", file=sys.stderr)
        return 1
    if "error" in data:
        print(f"rpc error: {data['error']}", file=sys.stderr)
        return 1

    groups = data.get("result") or []
    account = os.environ.get("SIGNAL_ACCOUNT", "")

    print("\nNote to Self  — always allowed, no configuration needed")
    if account:
        print(f"  {account}\n")

    if not groups:
        print("No groups visible to this device.\n")
    else:
        print(f"Groups visible to aaka ({len(groups)}):\n")
        for g in groups:
            gid = str(g.get("id") or "")
            name = g.get("name") or "(no name)"
            members = len(g.get("members") or [])
            mark = "ALLOWED " if ({gid, f"group:{gid}"} & allowed) else "silent  "
            print(f"  [{mark}] {name}")
            print(f"             members: {members}")
            print(f"             id: {gid}\n")

    # Contacts: how you find a person's id. Signal identifies people by uuid as
    # well as by number, and number sharing is optional — for many contacts the
    # uuid is the ONLY id you get, so it is what goes in the allowlist.
    try:
        cdata = _rpc("listContacts")
        contacts = cdata.get("result") or []
    except Exception:
        contacts = []

    people = []
    for c in contacts:
        num = str(c.get("number") or "").strip()
        uid = str(c.get("uuid") or "").strip()
        if account and (num == account or uid in account):
            continue  # that's us
        name = (c.get("name") or c.get("nickName") or c.get("givenName")
                or c.get("profileName") or "").strip()
        if not (num or uid):
            continue
        people.append((name, num, uid))

    if people:
        print(f"People aaka can see ({len(people)}):\n")
        for name, num, uid in sorted(people, key=lambda p: (not p[0], p[0].lower())):
            mark = "ALLOWED " if ({num, uid} - {""}) & allowed else "silent  "
            print(f"  [{mark}] {name or '(no profile name)'}")
            if num:
                print(f"             number: {num}")
            print(f"             uuid:   {uid or '(unknown)'}\n")
        print("Use the number when you have it, otherwise the uuid — either")
        print("works in the allowlist.\n")

    print("To let aaka speak in a chat, add its id to .env:\n")
    print("  SIGNAL_ALLOWED_CHATS=<id>[,<id>…]\n")
    print("Anything not listed is silent. A member messaging you privately is")
    print("NOT automatically allowed — on a linked device the reply would look")
    print("like it came from you.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
