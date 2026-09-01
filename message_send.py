#!/usr/bin/env python3
"""
message_send — send alerts via WhatsApp or Telegram.

Routes through gateway/adapter.py → gateway.egress (native: Telegram HTTP,
WhatsApp via the wa-sidecar, Slack Web API). No OpenClaw.

Usage (CLI):
  ./message_send.py "Hello, world!"
  ./message_send.py --channel telegram --to YOUR_TELEGRAM_ID "Alert: sync failed"
  ./message_send.py --channel whatsapp --to family-group "Dinner at 7pm"
  ./message_send.py --to self-dm "Quick note"

Usage (module):
  from message_send import send
  send("Alert!", channel="telegram")
  send("Hello group", recipient="family-group")

Config: $AAKA_CONFIG_DIR/$AAKA_CONTEXT/config/message_send.json
  {
    "default_channel": "telegram",
    "default_to": "YOUR_TELEGRAM_ID",
    "recipients": {
      "me":           { "channel": "telegram",  "to": "YOUR_TELEGRAM_ID" },
      "family-group": { "channel": "whatsapp",  "to": "120363...@g.us" },
      "self-dm":      { "channel": "whatsapp",  "to": "+491512..." }
    }
  }
"""

import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent
)
sys.path.insert(0, str(BASE))
import aaka_config

CONFIG = aaka_config.CONFIG_DIR / "config" / "message_send.json"


def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {}


def resolve_recipient(cfg: dict, to_arg: str | None, channel_arg: str | None) -> tuple[str, str]:
    """
    Resolve (channel, target) from args + config.
    `to_arg` may be a named recipient key (e.g. 'family-group') or a raw ID.
    Returns (channel, target) strings.
    """
    recipients = cfg.get("recipients", {})

    if to_arg and to_arg in recipients:
        rec = recipients[to_arg]
        channel = channel_arg or rec.get("channel") or cfg.get("default_channel", "telegram")
        target  = rec["to"]
    else:
        channel = channel_arg or cfg.get("default_channel", "telegram")
        target  = to_arg      or cfg.get("default_to", "")

    if not target:
        raise ValueError(
            "No recipient specified. Pass --to <id-or-name> or set default_to in message_send.json"
        )

    return channel, target


def send(
    message: str,
    channel: str | None = None,
    to: str | None = None,
    *,
    silent: bool = False,
    dry_run: bool = False,
    reply_to_message_id: str | None = None,
    # Executor-style aliases
    channel_id: str | None = None,
    sender: str | None = None,
) -> bool:
    """
    Send a message via the configured gateway.

    Returns True on success, False on failure (also prints error to stderr).
    """
    from gateway.adapter import GatewayAdapter

    # Support executor-style kwargs: channel_id → to (reply target), sender → fallback to
    if channel_id and not to:
        to = channel_id
    elif sender and not to:
        to = sender

    cfg = load_config()
    try:
        resolved_channel, target = resolve_recipient(cfg, to, channel)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False

    try:
        GatewayAdapter().send_message(
            resolved_channel, target, message,
            silent=silent, dry_run=dry_run,
            reply_to_message_id=reply_to_message_id,
        )
        return True
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Send a message via WhatsApp or Telegram through the configured gateway.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Named recipients (from config/message_send.json):
  me            Telegram DM
  family-group  WhatsApp Family Group
  self-dm       WhatsApp Self DM

Examples:
  %(prog)s "Dinner at 7"
  %(prog)s --to family-group "Dinner at 7"
  %(prog)s --channel telegram --to YOUR_TELEGRAM_ID "Alert!"
  %(prog)s --channel whatsapp --to +4915123146203 "Hello"
""",
    )
    parser.add_argument("message", help="Message text to send")
    parser.add_argument("--channel", help="Channel: telegram | whatsapp")
    parser.add_argument("--to", help="Recipient: named key, chat ID, E.164 phone, or group JID")
    parser.add_argument("--silent", action="store_true", help="Send silently (no notification)")
    parser.add_argument("--dry-run", action="store_true", help="Print payload without sending")
    args = parser.parse_args()

    ok = send(args.message, channel=args.channel, to=args.to, silent=args.silent, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
