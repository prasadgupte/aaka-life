#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0
"""
record_cassette.py — dev-only tool to record a demo cassette.

Drives the in-process sensor against samples/demo/, captures every user
message + bot reply with wall-clock timing, and writes a JSON cassette
plus a manifest entry for the static player to load.

Usage:
    AAKA_CONFIG_DIR=$(pwd)/samples/demo \
        python3 executor/webui/record_cassette.py

Inside the recorder:
    > /today
    🌤️ Aaka: ...
    > t milk
    > /done           # finish recording
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"
MANIFEST = CASSETTE_DIR / "manifest.json"
SCHEMA = "aaka-cassette/v0"


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s or "untitled"


def _load_members() -> dict:
    """Return {member_id: {name, emoji?, telegram?}} from demo config."""
    sys.path.insert(0, str(REPO_ROOT))
    import aaka_config
    out = {}
    for m in aaka_config.members():
        out[m["id"]] = {
            "name": m.get("name") or m["id"].capitalize(),
            "telegram": str(m.get("telegram") or ""),
            "emoji": m.get("emoji") or "",
        }
    return out


def _bot_identity() -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    import aaka_config
    return {"name": aaka_config.bot_name(), "emoji": aaka_config.bot_emoji()}


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"▸ {label}{suffix}: ").strip()
    return val or default


def _next_message(members: dict, current_member: str) -> tuple[str, str, dict | None]:
    """Return (text, member_id, attachment_or_none). Returns ('', '', None) on /done."""
    raw = input(f"{current_member}> ").rstrip()
    if not raw:
        return "", current_member, None
    if raw == "/done":
        return "", "", None

    # Member switch: ":member_id" or ":alex" prefix
    if raw.startswith(":"):
        parts = raw.split(None, 1)
        new_member = parts[0][1:]
        if new_member not in members:
            print(f"   (unknown member {new_member!r} — keeping {current_member})")
        else:
            current_member = new_member
        raw = parts[1] if len(parts) > 1 else ""
        if not raw:
            return "", current_member, None

    # Attachment shorthand: prefix "@<path>" attaches a file to this message
    attachment = None
    m = re.match(r"@(\S+)\s*(.*)", raw)
    if m:
        path = m.group(1)
        rest = m.group(2)
        p = Path(path)
        if not p.exists():
            print(f"   (file not found: {path} — sending text-only)")
        else:
            size = p.stat().st_size
            kind = (
                "image" if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
                else "pdf" if p.suffix.lower() == ".pdf"
                else "file"
            )
            attachment = {
                "type": kind,
                "name": p.name,
                "url": f"assets/{p.name}",
                "size_bytes": size,
            }
        raw = rest

    return raw, current_member, attachment


def _drive_sensor(message: str, sender_telegram: str) -> str:
    """Call the in-process sensor with the given sender. Returns the bot reply text."""
    sys.path.insert(0, str(REPO_ROOT))
    os.environ["OPENCLAW_SENDER"] = sender_telegram
    from sensor.router_sensor import route
    return route(message, dry_run=False) or ""


def _save(cassette: dict, name: str) -> Path:
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    out = CASSETTE_DIR / f"{name}.json"
    out.write_text(json.dumps(cassette, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def _update_manifest(name: str, cassette: dict) -> None:
    entries = []
    if MANIFEST.exists():
        try:
            entries = json.loads(MANIFEST.read_text())
        except json.JSONDecodeError:
            entries = []
    entries = [e for e in entries if e.get("name") != name]
    last_offset = cassette["messages"][-1]["ts_offset_ms"] if cassette["messages"] else 0
    entries.append({
        "name": name,
        "title": cassette["title"],
        "summary": cassette.get("summary", ""),
        "duration_estimate_ms": last_offset + 1500,
    })
    entries.sort(key=lambda e: e["name"])
    MANIFEST.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    if not os.environ.get("AAKA_CONFIG_DIR"):
        print("ERROR: AAKA_CONFIG_DIR must be set. Run like:")
        print(f"  AAKA_CONFIG_DIR={REPO_ROOT}/samples/demo python3 {__file__}")
        return 1

    members = _load_members()
    bot = _bot_identity()

    print(f"Aaka cassette recorder · config: {os.environ['AAKA_CONFIG_DIR']}")
    print(f"Bot: {bot['emoji']} {bot['name']}")
    print(f"Members: {', '.join(members)}")
    print("Commands inside: '/done' to save, ':<member>' to switch sender, '@<path> <msg>' to attach.")
    print()

    title = _prompt("Title", "Untitled tour")
    summary = _prompt("Summary (one line)", "")
    default_member = next(iter(members))
    current_member = _prompt("Starting member", default_member)
    if current_member not in members:
        print(f"   (unknown member, falling back to {default_member})")
        current_member = default_member

    messages: list[dict] = []
    t0 = time.monotonic()

    print()
    print("Begin. Type /done when finished.")
    print()

    while True:
        try:
            text, current_member, attachment = _next_message(members, current_member)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not current_member:
            break
        if not text and not attachment:
            continue

        offset = int((time.monotonic() - t0) * 1000)
        user_turn = {
            "from": "user",
            "member_id": current_member,
            "text": text,
            "ts_offset_ms": offset,
        }
        if attachment:
            user_turn["kind"] = "file"
            user_turn["attachment"] = attachment
        messages.append(user_turn)

        sender_tg = members[current_member].get("telegram") or ""
        if not sender_tg:
            print(f"   (member {current_member} has no telegram id — sensor will reject)")
            continue

        t_before = time.monotonic()
        try:
            reply = _drive_sensor(text, sender_tg)
        except Exception as e:
            reply = f"⚠️ recorder error: {e}"
        latency_ms = int((time.monotonic() - t_before) * 1000)

        if reply.strip():
            print(f"{bot['emoji']} {bot['name']}: {reply.splitlines()[0][:140]}" + ("..." if len(reply) > 140 else ""))
            bot_offset = int((time.monotonic() - t0) * 1000)
            messages.append({
                "from": "bot",
                "text": reply,
                "ts_offset_ms": bot_offset,
                "typing_ms": min(900, max(200, latency_ms + 200)),
            })
        else:
            print("   (no reply)")

    if not messages:
        print("Nothing recorded — exiting without saving.")
        return 0

    cassette = {
        "schema": SCHEMA,
        "title": title,
        "summary": summary,
        "bot": bot,
        "members": members,
        "messages": messages,
    }
    name = _slug(title)
    path = _save(cassette, name)
    _update_manifest(name, cassette)
    print()
    print(f"Saved: {path.relative_to(REPO_ROOT)} ({len(messages)} turns)")
    print(f"Manifest updated: {MANIFEST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
