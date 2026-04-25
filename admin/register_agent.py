#!/usr/bin/env python3
"""
Register a new agent with the Aaka gateway.

Usage:
    python3 admin/register_agent.py <agent-id> "<Display Name>" --location /path/to/agent.py
    python3 admin/register_agent.py <agent-id> "<Display Name>" --location /path/to/agent.py --expires 365
    python3 admin/register_agent.py <agent-id> "<Display Name>" --allow-dangerous
    python3 admin/register_agent.py --list
    python3 admin/register_agent.py --revoke <agent-id>

The API key is printed once and NOT stored (only SHA-256 hash kept in DB).
Re-registering an existing agent ID rotates the key and updates location/expiry.

--allow-dangerous grants the agent permission to run Claude with --dangerously-skip-permissions.
Only use this for fully trusted agents running in isolated project directories.
"""
import argparse
import os
import secrets
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# Ensure QUEUE_DB points to the config-dir DB (not repo-local fallback)
import aaka_config  # noqa: E402
if "QUEUE_DB" not in os.environ:
    os.environ["QUEUE_DB"] = str(aaka_config.QUEUE_DIR / "butler.db")

from aaka_queue.queue import _connect, register_agent

DEFAULT_EXPIRY_DAYS = 365


def _expiry_ts(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_register(agent_id: str, display_name: str, location: str | None,
                  expires_days: int | None, allow_dangerous: bool = False,
                  rate_limit: int = 60) -> None:
    import json as _json
    api_key = "aaka-" + secrets.token_urlsafe(32)
    key_expires_at = _expiry_ts(expires_days) if expires_days else None
    permissions = _json.dumps({"allow_dangerous": True}) if allow_dangerous else "{}"

    register_agent(
        agent_id,
        display_name,
        api_key,
        location=location,
        key_expires_at=key_expires_at,
    )
    # Set permissions and rate limit columns
    with _connect() as conn:
        conn.execute(
            "UPDATE agent_registry SET permissions=?, rate_limit_per_hour=? WHERE id=?",
            (permissions, rate_limit, agent_id),
        )

    expiry_note = f"Expires: {key_expires_at}" if key_expires_at else "Expires: never"
    loc_note = f"Location: {location}" if location else "Location: not set"
    danger_note = "\n  ⚠️  allow_dangerous=true — Claude --dangerously-skip-permissions enabled" if allow_dangerous else ""
    rate_note = f"Rate limit: {rate_limit} msg/hour"

    print(f"\nAgent registered: {agent_id!r} ({display_name})")
    print(f"  {loc_note}")
    print(f"  {expiry_note}")
    print(f"  {rate_note}{danger_note}")
    print(f"\nAPI Key (save this — it will NOT be shown again):")
    print(f"  {api_key}")
    print(f"\nSet in agent env:  export AAKA_AGENT_KEY='{api_key}'")
    print(f"Or in .env file:   AAKA_AGENT_KEY={api_key}\n")


def cmd_list() -> None:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, display_name, active, created_at, last_used_at, location, key_expires_at "
            "FROM agent_registry ORDER BY created_at"
        ).fetchall()
    if not rows:
        print("No agents registered.")
        return
    print(f"\n{'ID':<24} {'Name':<26} {'Active':<8} {'Last Used':<22} {'Expires':<22} Location")
    print("-" * 120)
    for r in rows:
        active = "yes" if r["active"] else "REVOKED"
        last = (r["last_used_at"] or "never")[:19]
        exp = (r["key_expires_at"] or "never")[:19]
        loc = r["location"] or "-"
        print(f"{r['id']:<24} {r['display_name']:<26} {active:<8} {last:<22} {exp:<22} {loc}")
    print()


def cmd_revoke(agent_id: str) -> None:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE agent_registry SET active=0 WHERE id=?", (agent_id,)
        )
    if cur.rowcount:
        print(f"Revoked: {agent_id}")
    else:
        print(f"Agent not found: {agent_id}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage Aaka agent registrations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("agent_id", nargs="?", help="Agent slug, e.g. 'travel-agent'")
    parser.add_argument("display_name", nargs="?", help="Human name, e.g. 'Travel Agent'")
    parser.add_argument("--location", metavar="PATH_OR_URL",
                        help="File path or URL where this agent's code lives")
    parser.add_argument("--expires", metavar="DAYS", type=int, default=DEFAULT_EXPIRY_DAYS,
                        help=f"Key lifetime in days (default: {DEFAULT_EXPIRY_DAYS}; 0 = never expires)")
    parser.add_argument("--list", action="store_true", help="List all registered agents")
    parser.add_argument("--revoke", metavar="AGENT_ID", help="Revoke an agent key")
    parser.add_argument("--allow-dangerous", action="store_true",
                        help="Grant agent permission to run Claude with --dangerously-skip-permissions")
    parser.add_argument("--rate-limit", metavar="MSGS_PER_HOUR", type=int, default=60,
                        help="Max messages per hour this agent may send (default: 60)")
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.revoke:
        cmd_revoke(args.revoke)
    elif args.agent_id and args.display_name:
        expires_days = None if args.expires == 0 else args.expires
        cmd_register(args.agent_id, args.display_name, args.location, expires_days,
                     allow_dangerous=args.allow_dangerous, rate_limit=args.rate_limit)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
