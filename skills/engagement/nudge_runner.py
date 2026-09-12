#!/usr/bin/env python3
"""
Aaka — Nudge Runner

Called every 15 minutes by launchd (com.aaka.nudgeengine).
For each active member, asks the nudge engine if anything is due,
then sends the message via the configured gateway.

Usage:
    python3 skills/engagement/nudge_runner.py
    python3 skills/engagement/nudge_runner.py --dry-run
    python3 skills/engagement/nudge_runner.py --member alex
"""

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [nudge] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nudge_runner")

import aaka_config

def _preferred_channel(member: dict) -> "tuple[str, str]":
    """(handle, channel) for the first *enabled* channel this member is reachable
    on — the same rule scheduled summaries and tool reports use. The outbox
    `source` we write is the channel name, so sensor/flush_outbox.py picks the
    matching egress adapter."""
    handle, channel = aaka_config.preferred_handle(member or {})
    return handle, channel or "telegram"


def _send_to_member(member_id: str, text: str, dry_run: bool = False) -> bool:
    """Send message to a member via their preferred channel."""
    m_obj = aaka_config.member_by_name(member_id)
    if not m_obj:
        log.warning("unknown member: %s", member_id)
        return False

    target, channel = _preferred_channel(m_obj)

    if not target:
        log.warning("no channel for member %s", member_id)
        return False

    if dry_run:
        log.info("[DRY-RUN] → %s (%s): %s", member_id, channel, text[:80])
        return True

    # Mac executor has no Telegram bot token — queue to outbox so the VPS
    # sensor flushes it through gateway.egress with the real credentials.
    from aaka_queue.queue import write_outbox
    try:
        write_outbox(
            channel_id=str(target),
            sender=str(target),
            text=text,
            source=channel,
            ttl_minutes=60,
        )
        log.info("queued nudge for %s (%s)", member_id, channel)
        return True
    except Exception as exc:
        log.warning("queue write failed for %s: %s", member_id, exc)
        return False


def run_for_member(member_id: str, dry_run: bool = False) -> None:
    from skills.engagement.nudge_engine import decide
    from skills.engagement.engagement_db import record_nudge

    decision = decide(member_id)
    if decision is None:
        return

    nudge_type = decision.nudge_type
    payload = decision.payload
    log.info("nudge decided: %s → %s", member_id, nudge_type)

    sent = False

    if nudge_type == "conflict_alert":
        for msg in payload.get("messages", []):
            sent = _send_to_member(member_id, msg, dry_run=dry_run) or sent

    elif nudge_type == "email_fallback":
        m_obj = aaka_config.member_by_name(member_id) or {}
        name = m_obj.get("name", member_id.capitalize())
        # Build upcoming events summary
        CALENDAR = aaka_config.CALENDAR_DIR
        cache_path = CALENDAR / "weekly_events.json"
        upcoming_lines = []
        if cache_path.exists():
            try:
                events = json.loads(cache_path.read_text())
                today = str(datetime.date.today())
                future = [e for e in events if e.get("date", "") >= today][:3]
                for ev in future:
                    upcoming_lines.append(f"  {ev.get('date','')}  {ev.get('title','')}")
            except Exception:
                pass
        upcoming_events = "\n".join(upcoming_lines) or "  (no events found)"

        from skills.engagement.templates import EMAIL_SUBJECT, EMAIL_BODY, EMAIL_CTA_GENERIC
        from skills.engagement.engagement_db import days_since_active
        bot = aaka_config.bot_name()
        days = int(days_since_active(member_id))
        body = EMAIL_BODY.format(
            name=name,
            days=days,
            upcoming_events=upcoming_events,
            cta=EMAIL_CTA_GENERIC,
            bot=bot,
        )
        subject = EMAIL_SUBJECT.format(bot=bot)
        from skills.engagement.email_sender import send_email
        if dry_run:
            log.info("[DRY-RUN] email → %s: %s", member_id, body[:120])
            sent = True
        else:
            sent = send_email(member_id, subject, body)

    elif nudge_type == "admin_alert":
        from skills.engagement.templates import ADMIN_ALERT
        m_obj = aaka_config.member_by_name(member_id) or {}
        text = ADMIN_ALERT.format(
            member_name=m_obj.get("name", member_id.capitalize()),
            days=payload.get("days", "?"),
            bot=aaka_config.bot_name(),
        )
        # Send to owner (first admin member)
        owner = next(
            (m for m in aaka_config.members() if m.get("admin") and m.get("id") != member_id),
            None,
        )
        if owner:
            target, channel = _preferred_channel(owner)
            if dry_run:
                log.info("[DRY-RUN] admin_alert → %s: %s", owner.get("id"), text)
                sent = True
            else:
                from aaka_queue.queue import write_outbox
                try:
                    write_outbox(
                        channel_id=str(target),
                        sender=str(target),
                        text=text,
                        source=channel,
                        ttl_minutes=60,
                    )
                    sent = True
                except Exception as exc:
                    log.warning("admin_alert queue write failed: %s", exc)
                    sent = False

    else:
        # All other types: text in payload
        text = payload.get("text", "")
        if text:
            sent = _send_to_member(member_id, text, dry_run=dry_run)

    if sent and not dry_run:
        record_nudge(member_id, nudge_type)


def main():
    parser = argparse.ArgumentParser(description="Aaka nudge runner")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--member", help="Run for one member only")
    args = parser.parse_args()

    from skills.engagement.engagement_db import purge_old_events
    purge_old_events()

    members = aaka_config.members()
    if args.member:
        members = [m for m in members if m.get("id") == args.member or m.get("name", "").lower() == args.member.lower()]

    for m in members:
        mid = m.get("id")
        if not mid:
            continue
        try:
            run_for_member(mid, dry_run=args.dry_run)
        except Exception as exc:
            log.error("error processing member %s: %s", mid, exc)


if __name__ == "__main__":
    main()
