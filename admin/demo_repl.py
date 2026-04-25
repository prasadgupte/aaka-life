#!/usr/bin/env python3
"""
admin/demo_repl.py — Aaka demo REPL for the Ash-Kaa family.

Imports sensor.router_sensor.route() directly — no Docker, no Telegram.
Env vars must be set before this script runs (done by demo.sh):
  AAKA_BASE, AAKA_CONFIG_DIR, QUEUE_DB

Special commands:
  /as <name>   Switch active sender (e.g. /as tsu, /as alex)
  /reset       Restore list + note seed data (git checkout)
  quit / exit  Exit
"""

import os
import re
import sys
import subprocess
from pathlib import Path

# ── Env validation ───────────────────────────────────────────────────────────
REPO_DIR   = Path(os.environ.get("AAKA_BASE", Path(__file__).resolve().parent.parent))
DEMO_DIR   = Path(os.environ.get("AAKA_CONFIG_DIR", REPO_DIR / "samples" / "demo"))
QUEUE_DB   = os.environ.get("QUEUE_DB", str(DEMO_DIR / "data" / "queue" / "butler.db"))

os.environ.setdefault("AAKA_BASE",       str(REPO_DIR))
os.environ.setdefault("AAKA_CONFIG_DIR", str(DEMO_DIR))
os.environ.setdefault("QUEUE_DB",        QUEUE_DB)

sys.path.insert(0, str(REPO_DIR))

# ── Load config + router (after env vars are set) ────────────────────────────
import aaka_config

def _member_sender_id(member: dict) -> str:
    """Return the best sender ID for a demo member (whatsapp → telegram → id)."""
    return member.get("whatsapp") or member.get("telegram") or member.get("id", "")


def _switch_member(name: str) -> tuple[dict | None, str]:
    """Find member by name/id and return (member, display_name). None if not found."""
    m = aaka_config.member_by_name(name)
    if m:
        return m, m.get("name", name)
    return None, ""


def _load_route():
    """Import route() lazily so env vars are already set."""
    from sensor.router_sensor import route
    return route


def _strip_prefix(text: str) -> str:
    """Strip the bot reply prefix (e.g. the value of aaka_config.reply_prefix())
    for cleaner REPL output."""
    prefix = aaka_config.reply_prefix()
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def _print_banner(current_member: dict):
    members = aaka_config.members()
    adults  = [m["name"] for m in members if m.get("role") == "adult"]
    kids    = [m["name"] for m in members if m.get("role") == "child"]
    others  = [m["name"] for m in members if m.get("role") not in ("adult", "child")]
    print()
    title = f"🌤️  {aaka_config.bot_name()} Demo — The Ash-Kaa Family"
    print("╔══════════════════════════════════════════════════╗")
    print(f"║  {title}".ljust(51) + "║")
    print("║                                                  ║")
    if adults:
        print(f"║  {', '.join(adults)} (adults)".ljust(51) + "║")
    if kids:
        print(f"║  {', '.join(kids)} (kids)".ljust(51) + "║")
    if others:
        print(f"║  {', '.join(others)}".ljust(51) + "║")
    print("║                                                  ║")
    print("║  Try:  t  w  b grocery  /tasks  /menu            ║")
    print("║  Switch:  /as tsu  /as alex                      ║")
    print("║  Reset:   /reset   Exit: quit                    ║")
    print("╚══════════════════════════════════════════════════╝")
    print()


def _reset_data():
    """Restore list + note seed data from git."""
    try:
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "checkout", "--",
             "samples/demo/data/lists/", "samples/demo/data/notes/"],
            check=True, capture_output=True,
        )
        Path(QUEUE_DB).unlink(missing_ok=True)
        print("  ✓ Demo data reset to seed state.")
    except Exception as e:
        print(f"  ⚠️  Reset failed: {e}")


def main():
    # Default sender: Alex
    members = aaka_config.members()
    current = next((m for m in members if m.get("id") == "alex"), members[0] if members else None)
    if current is None:
        print("ERROR: no members in demo config")
        sys.exit(1)

    os.environ["OPENCLAW_SENDER"] = _member_sender_id(current)
    route = _load_route()

    _print_banner(current)

    while True:
        name = current.get("name", current.get("id", "?")).lower()
        try:
            raw = input(f"{name}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not raw:
            continue

        # ── Built-in REPL commands ───────────────────────────────────────────
        if raw.lower() in ("quit", "exit", "q"):
            print("Bye.")
            break

        if raw.lower() == "/reset":
            _reset_data()
            continue

        m = re.match(r"^/as\s+(\S+)", raw, re.IGNORECASE)
        if m:
            target = m.group(1).lower()
            found, display = _switch_member(target)
            if found:
                current = found
                os.environ["OPENCLAW_SENDER"] = _member_sender_id(current)
                print(f"  → Switched to {display}.\n")
            else:
                valid = ", ".join(m.get("id", "") for m in members)
                print(f"  Unknown member '{target}'. Try: {valid}\n")
            continue

        # ── Route through sensor ─────────────────────────────────────────────
        try:
            reply = route(raw)
        except Exception as exc:
            # Typically: LLM key not set for queue-bound intents
            if "api" in str(exc).lower() or "key" in str(exc).lower() or "gemini" in str(exc).lower():
                print("  ↳ This intent needs LLM extraction.")
                print("    Set GEMINI_API_KEY to try it live.\n")
            else:
                print(f"  ⚠️  {exc}\n")
            continue

        if not reply:
            print("  (no response — sender may not be recognised)\n")
            continue

        clean = _strip_prefix(reply)
        print()
        print(clean)
        print()


if __name__ == "__main__":
    main()
