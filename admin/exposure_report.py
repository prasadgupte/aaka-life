#!/usr/bin/env python3
"""aaka exposure report — an attack-surface *inventory* (not pass/fail).

Complements admin/security_check.py: that one asserts hardening; this one maps
what exists — what can reach IN (listening ports, public endpoints) and what each
credential can reach OUT to / do (token scopes → plain-English capability),
plus the secrets/scraping-cred inventory (names only, never values).

    python3 admin/exposure_report.py          # human-readable
    python3 admin/exposure_report.py --json    # machine-readable

Surfaced in chat as `/security surface` (admin-only) and MCP `exposure_report()`.
Read-only. Add rows here as new surfaces appear (living runbook).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR", "/Users/Shared/aaka-repo-config"))
SECRETS_ROOT = Path(os.environ.get("AAKA_SECRETS_ROOT", "/Users/Shared/secrets"))
HOST = socket.gethostname()

# port → (service, is this normally VPS-public?) — from the CLAUDE.md port registry
KNOWN_PORTS = {
    8002: ("Aaka Taskboard UI", False), 8003: ("Aaka console/dashboard", False),
    18790: ("Agent Gateway API", False), 18792: ("WhatsApp sidecar (Baileys)", False),
    18793: ("WhatsApp inbound receiver", False), 19000: ("Aaka sensor container", False),
    8000: ("Playlist app", False), 8001: ("TOTP auth", False),
}
# high-impact OAuth scopes → what possessing this token lets you DO
SCOPE_CAP = {
    "gmail.send": ("SEND email as this account", True),
    "gmail.modify": ("modify mail — labels/read/trash", False),
    "gmail.readonly": ("read mail", False),
    "gmail.labels": ("manage mail labels", False),
    "calendar.events": ("create/edit/DELETE calendar events", True),
    "calendar.readonly": ("read calendar", False),
    "tasks": ("read/write tasks", False),
    "photoslibrary.readonly": ("read photos", False),
    "photospicker.mediaitems.readonly": ("read picked photos", False),
}
# VPS Caddy-exposed routes (public HTTPS) — from the registry
PUBLIC_ENDPOINTS = [
    ("your-vps-host.example/play/*", "Playlist app", "TOTP 2FA"),
    ("your-vps-host.example/auth/*", "TOTP auth service", "none"),
    ("your-vps-host.example :18789 (internal)", "Aaka sensor", "OpenClaw-only / not public"),
]


def _listening() -> list[dict]:
    """Listening TCP sockets with bind scope. 0.0.0.0/:: = network-reachable."""
    out = []
    try:
        r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=6)
        lines = r.stdout.splitlines()[1:]
    except Exception:
        try:
            r = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True, timeout=6)
            lines = r.stdout.splitlines()
        except Exception:
            return out
    seen = set()
    for ln in lines:
        m = re.search(r"(\S+):(\d+)\s+\(LISTEN\)", ln) or re.search(r"(\S+):(\d+)\s*$", ln)
        if not m:
            continue
        addr, port = m.group(1), int(m.group(2))
        if port in seen:
            continue
        seen.add(port)
        public = addr in ("*", "0.0.0.0", "::", "[::]") or addr.endswith("0.0.0.0")
        svc = KNOWN_PORTS.get(port, ("unknown", False))[0]
        out.append({"port": port, "bind": addr, "network_reachable": public, "service": svc})
    return sorted(out, key=lambda x: x["port"])


def _tokens() -> list[dict]:
    tdir = CONFIG_DIR / "tokens"
    res = []
    if not tdir.exists():
        return res
    for f in sorted(tdir.glob("*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        scopes = d.get("scopes") or (d.get("scope").split() if isinstance(d.get("scope"), str) else [])
        if not scopes:
            continue  # client file / non-token
        caps, high = [], False
        for s in scopes:
            key = s.split("auth/")[-1]
            cap, hi = SCOPE_CAP.get(key, (key, False))
            caps.append(("⚠️ " if hi else "") + cap)
            high = high or hi
        online = "vps" in f.name.lower()
        res.append({"token": f.name, "online": online, "high_impact": high, "caps": caps})
    return res


def _secrets() -> list[str]:
    items = []
    if SECRETS_ROOT.exists():
        items += [f"{p.name}/ (vault)" for p in sorted(SECRETS_ROOT.iterdir()) if p.is_dir()]
    tools_yaml = CONFIG_DIR / "config" / "tools.yaml"
    if tools_yaml.exists():
        try:
            import yaml
            man = yaml.safe_load(tools_yaml.read_text()) or {}
            for name, e in man.items():
                if isinstance(e, dict) and e.get("secrets"):
                    items.append(f"tool:{name} → scraping creds ({e['secrets']})")
        except Exception:
            pass
    return items


def run() -> dict:
    listening = _listening()
    return {
        "host": HOST,
        "listening": listening,
        "network_reachable": [s for s in listening if s["network_reachable"]],
        "public_endpoints": [{"url": u, "service": s, "auth": a} for u, s, a in PUBLIC_ENDPOINTS],
        "tokens": _tokens(),
        "secrets": _secrets(),
    }


def _human(o: dict) -> str:
    L = [f"🗺️ *Exposure map* — {o['host']}"]
    L.append("\n*Inbound — listening ports*")
    for s in o["listening"]:
        flag = "🌐 NETWORK-REACHABLE" if s["network_reachable"] else "🔒 localhost"
        L.append(f"  {flag} :{s['port']} — {s['service']} (bind {s['bind']})")
    if not o["listening"]:
        L.append("  (none detected)")
    L.append("\n*Public endpoints (VPS)*")
    for e in o["public_endpoints"]:
        L.append(f"  • {e['url']} — {e['service']} · auth: {e['auth']}")
    L.append("\n*Tokens — what each can DO*")
    for t in o["tokens"]:
        tag = ("🌐 online" if t["online"] else "🏠 local") + (" · ⚠️ high-impact" if t["high_impact"] else "")
        L.append(f"  • {t['token']} [{tag}]")
        L.append(f"      {' · '.join(t['caps'])}")
    L.append("\n*Secrets / scraping creds* (names only)")
    for s in o["secrets"]:
        L.append(f"  • {s}")
    reachable = len(o["network_reachable"])
    L.append(f"\n{reachable} network-reachable port(s) · {len(o['tokens'])} tokens · {len(o['secrets'])} secret stores")
    if reachable:
        L.append("⚠️ Review network-reachable ports — anything not meant for the LAN/internet should bind 127.0.0.1.")
    return "\n".join(L)


if __name__ == "__main__":
    o = run()
    print(json.dumps(o, indent=2) if "--json" in sys.argv else _human(o))
    sys.exit(0)
