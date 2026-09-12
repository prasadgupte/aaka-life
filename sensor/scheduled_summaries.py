#!/usr/bin/env python3
"""
Aaka — Scheduled Summaries

Invoked by cron every hour. Checks the current time in the configured
timezone and sends appropriate calendar summaries to the family group
and individual members.

Schedule (all times in configured timezone, default Europe/Berlin):
  Saturday 17:00  — Heads-up about the upcoming week
  Sunday   19:00  — Weekly overview with conflict analysis
  Daily    21:00  — Tomorrow's summary

Messages are written to the outbox table; flush_outbox.py (cron every 3 min)
handles the actual send via OpenClaw.

Usage:
  python3 sensor/scheduled_summaries.py --check          # gate on time, run if due
  python3 sensor/scheduled_summaries.py --saturday       # force Saturday heads-up
  python3 sensor/scheduled_summaries.py --sunday          # force Sunday overview
  python3 sensor/scheduled_summaries.py --tomorrow        # force daily tomorrow
  python3 sensor/scheduled_summaries.py --check --dry-run # print without sending
"""

import argparse, datetime, json, os, sys
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))

import aaka_config
from aaka_queue.queue import write_outbox
from aaka_config import reply_prefix

REPLY_PREFIX = reply_prefix()
CALENDAR_DIR = aaka_config.CALENDAR_DIR
WEEKLY_CACHE = CALENDAR_DIR / "weekly_events.json"


def _tz() -> ZoneInfo:
    return ZoneInfo(aaka_config.timezone())


def _now_local() -> datetime.datetime:
    return datetime.datetime.now(_tz())


def _telegram_group_id() -> str:
    return os.environ.get("TELEGRAM_GROUP_ID", "")


def _member_targets() -> dict[str, tuple[str, str]]:
    """{member_id: (handle, channel)} — each member's preferred *enabled* channel
    (telegram → whatsapp → signal), so a Signal-only kid gets the same pushes a
    Telegram parent does. The channel is written as the outbox `source`."""
    return aaka_config.member_targets()


def _read_md(filename: str) -> str:
    path = CALENDAR_DIR / filename
    return path.read_text().strip() if path.exists() else ""


def _format_tomorrow_events() -> str:
    """Build a summary of tomorrow's events from the weekly cache."""
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    if not WEEKLY_CACHE.exists():
        return ""
    try:
        cache = json.loads(WEEKLY_CACHE.read_text())
    except Exception:
        return ""

    events = [e for e in cache if e.get("date") == tomorrow]
    if not events:
        return "Nothing on the calendar tomorrow."

    events.sort(key=lambda e: e.get("start", ""))
    try:
        d = datetime.date.fromisoformat(tomorrow)
        header = d.strftime("**%A** %-d %B %Y")
    except Exception:
        header = tomorrow

    lines = [header]
    for ev in events:
        start = ev.get("start", "")
        end = ev.get("end", "")
        emoji = ev.get("emoji", "")
        title = ev.get("title", "")
        time_str = f"**{start}**" if start else ""
        dur = ""
        if start and end:
            try:
                t1 = datetime.datetime.strptime(start, "%H:%M")
                t2 = datetime.datetime.strptime(end, "%H:%M")
                mins = int((t2 - t1).total_seconds() / 60)
                if mins >= 60:
                    dur = f"({mins // 60}h{'%02d' % (mins % 60) if mins % 60 else ''})"
                else:
                    dur = f"({mins}m)"
            except Exception:
                pass
        loc = ev.get("location", "")
        loc_str = f" 📍 {loc}" if loc else ""
        prefix = f"{emoji} " if emoji else "- "
        lines.append(f"{prefix}{time_str} {dur} {title}{loc_str}".strip())
    return "\n".join(lines)


def _format_member_tomorrow(member_id: str) -> str:
    """Build tomorrow's summary for a specific member from their weekly file."""
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    # Try per-member weekly file
    content = _read_md(f"weekly_{member_id}.md")
    if not content:
        return ""

    # Extract tomorrow's block from the weekly markdown
    lines = content.split("\n")
    collecting = False
    result = []
    for line in lines:
        # Date headers look like "**Monday** 28 April 2026"
        if line.startswith("**") and "**" in line[2:]:
            try:
                # Parse the date from the header
                date_part = line.split("**")[-1].strip()
                d = _parse_md_date(date_part)
                if d and d.isoformat() == tomorrow:
                    collecting = True
                    result.append(line)
                    continue
                elif collecting:
                    break  # Next day header — stop
            except Exception:
                pass
        if collecting:
            result.append(line)

    return "\n".join(result).strip() if result else ""


def _parse_md_date(text: str) -> datetime.date | None:
    """Try to parse '28 April 2026' or similar from weekly.md headers."""
    import re
    m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', text)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y").date()
    except Exception:
        return None


def _fix_summary(period: str, member_id: str | None = None) -> str:
    """Return formatted fix issues, or empty string if none."""
    try:
        from skills.calendar.fix_analyzer import analyze_fix, format_fix_list
        issues = analyze_fix(period, member_id=member_id)
        if issues:
            return "\n\n" + format_fix_list(issues)
    except Exception:
        pass
    return ""


def _send_to_outbox(channel_id: str, text: str, source: str = "telegram",
                    dry_run: bool = False, reply_markup: "dict | None" = None) -> None:
    if dry_run:
        print(f"[dry-run] → {channel_id}:\n{text}\n{'─' * 40}")
        return
    write_outbox(channel_id=channel_id, sender="scheduled", text=text, source=source,
                 ttl_minutes=60, silent=True, reply_markup=reply_markup)


# ── Summary generators ────────────────────────────────────────────────────────

def send_saturday_heads_up(dry_run: bool = False) -> None:
    """Saturday 17:00: heads-up about the upcoming week."""
    from skills.calendar.fix_analyzer import analyze_fix, merge_fixes_inline

    group_id = _telegram_group_id()
    content = _read_md("weekly.md")
    if not content:
        print("[skip] No weekly.md available")
        return

    group_issues = analyze_fix("week")
    content_merged = merge_fixes_inline(content, group_issues)
    text = f"{REPLY_PREFIX}📣 Heads up — here's what's coming up this week:\n\n{content_merged}"

    if group_id:
        _send_to_outbox(group_id, text, dry_run=dry_run)

    # Per-member DM
    targets = _member_targets()
    for mid, (handle, channel) in targets.items():
        member_content = _read_md(f"weekly_{mid}.md") or content
        member_issues = analyze_fix("week", member_id=mid)
        member_merged = merge_fixes_inline(member_content, member_issues)
        member_text = f"{REPLY_PREFIX}📣 Your week ahead:\n\n{member_merged}"
        _send_to_outbox(handle, member_text, source=channel, dry_run=dry_run)

    print(f"[ok] Saturday heads-up queued (group={'yes' if group_id else 'no'}, members={len(targets)})")


def _weekly_completion_digest() -> str:
    """Build a compact weekly completion digest for Sunday push."""
    from skills.tasks.local_tasks import list_completed
    done = list_completed(since="week")
    if not done:
        return ""
    # Group by day
    by_day: dict[str, list[str]] = {}
    for t in done:
        day = t.get("done_at", "")
        by_day.setdefault(day, []).append(t.get("title", ""))
    lines = [f"\n📊 This week: {len(done)} task{'s' if len(done) != 1 else ''} completed"]
    import datetime as _dt
    for day_iso in sorted(by_day.keys()):
        try:
            d = _dt.date.fromisoformat(day_iso)
            day_label = d.strftime("%a")
        except ValueError:
            day_label = day_iso
        titles = ", ".join(by_day[day_iso][:3])
        if len(by_day[day_iso]) > 3:
            titles += f" +{len(by_day[day_iso]) - 3}"
        lines.append(f"  {day_label}: {titles}")
    return "\n".join(lines)


def send_sunday_overview(dry_run: bool = False) -> None:
    """Sunday 19:00: weekly overview with conflict analysis."""
    from skills.calendar.fix_analyzer import analyze_fix, merge_fixes_inline

    group_id = _telegram_group_id()
    content = _read_md("weekly.md")
    if not content:
        print("[skip] No weekly.md available")
        return

    group_issues = analyze_fix("week")
    content_merged = merge_fixes_inline(content, group_issues)
    completion_digest = _weekly_completion_digest()
    text = f"{REPLY_PREFIX}📅 Week at a glance:\n\n{content_merged}{completion_digest}"

    if group_id:
        _send_to_outbox(group_id, text, dry_run=dry_run)

    targets = _member_targets()
    for mid, (handle, channel) in targets.items():
        member_content = _read_md(f"weekly_{mid}.md") or content
        member_issues = analyze_fix("week", member_id=mid)
        member_merged = merge_fixes_inline(member_content, member_issues)
        member_text = f"{REPLY_PREFIX}📅 Your week:\n\n{member_merged}{completion_digest}"
        _send_to_outbox(handle, member_text, source=channel, dry_run=dry_run)

    print(f"[ok] Sunday overview queued (group={'yes' if group_id else 'no'}, members={len(targets)})")


def send_birthday_morning(dry_run: bool = False) -> None:
    """Daily 07:00: per-member birthday DM for birthdays in the next 7 days.
    DEPRECATED — replaced by send_daily_morning() which includes birthdays.
    Kept for --birthday CLI flag backward compat.
    """
    from skills.contacts.birthday_list import query as bday_query
    targets = _member_targets()
    if not targets:
        print("[skip] No members reachable on an enabled channel")
        return

    sent = 0
    for mid, (handle, channel) in targets.items():
        text = bday_query("", sender_id=handle)  # saves numbered list under the handle
        if "No birthdays" in text or "No birthday data" in text:
            continue
        full_text = f"{REPLY_PREFIX}🎂 Birthdays coming up:\n\n{text}"
        _send_to_outbox(handle, full_text, source=channel, dry_run=dry_run)
        sent += 1

    print(f"[ok] Birthday morning DM queued for {sent}/{len(targets)} member(s)")


def _member_emoji(member: dict) -> str:
    """Pick a display emoji for a member from their calendar config or role."""
    cals = member.get("calendars", {})
    if "personal" in cals:
        return cals["personal"].get("emoji", "")
    _ROLE_EMOJI = {"child": "🧒", "grandparent": "👵", "adult": "👤"}
    return _ROLE_EMOJI.get(member.get("role", ""), "•")


_BDAY_SPLIT = "\n\n🎂 Today's birthdays:"
# Matches either bare HH:MM at line start (e.g. "09:00 Standup") or **HHMM**
# anywhere on a line (voice-template style: "🗳️❗**1430** *Alex*..."). We need
# the line start, so the regex captures (start-of-line, content-up-to-time).
_FIRST_EVENT_RE = __import__("re").compile(
    r"^(?=.*(?:\*\*\d{4}\*\*|\b\d{2}:\d{2}\b))", __import__("re").MULTILINE
)


def _flag_first_event(schedule: str) -> str:
    """Prepend 🏁 to the first line containing a time (HH:MM or **HHMM**).

    The voice template renders times as **HHMM**; raw sidecar lines use HH:MM.
    All-day events have no time and are skipped over.
    """
    for m in _FIRST_EVENT_RE.finditer(schedule):
        start = m.start()
        # Avoid empty lines and the line right after a header break
        line_end = schedule.find("\n", start)
        line = schedule[start:line_end if line_end != -1 else len(schedule)]
        if not line.strip():
            continue
        return schedule[:start] + "🏁 " + schedule[start:]
    return schedule


def _split_birthdays(schedule: str) -> "tuple[str, str]":
    """Split schedule into (calendar_part, birthday_part).
    birthday_part is empty if no birthdays section is present."""
    idx = schedule.find(_BDAY_SPLIT)
    if idx == -1:
        return schedule, ""
    return schedule[:idx], schedule[idx:]


def send_daily_morning(dry_run: bool = False) -> None:
    """Daily 07:00: scheduled /t push.

    Each member sees:
      ☀️ <date>
      <their schedule, with 🏁 on the first time-bearing event>
      <task block>
      <today's birthdays — last so tasks take priority>
    """
    from sensor.router_sensor import _build_today_schedule

    targets = _member_targets()
    if not targets:
        print("[skip] No members reachable on an enabled channel")
        return

    date_header = _now_local().strftime("%a %-d %b")
    header = f"{REPLY_PREFIX}☀️ {date_header}"

    from skills.tasks.local_tasks import summary_for_push
    task_block = summary_for_push()

    for mid, (handle, channel) in targets.items():
        schedule = _build_today_schedule(mid, sender=handle, include_tasks=False)
        calendar_part, bday_part = _split_birthdays(schedule)
        calendar_part = _flag_first_event(calendar_part)

        text = header + "\n\n" + calendar_part
        if task_block:
            text += "\n\n" + task_block
        if bday_part:
            text += bday_part  # already starts with \n\n🎂 ...

        _send_to_outbox(handle, text, source=channel, dry_run=dry_run)

    print(f"[ok] Daily morning queued for {len(targets)} member(s)")


def send_birthday_auto(dry_run: bool = False) -> None:
    """Daily 07:00: queue birthday wish confirmations for today's birthdays.

    For each contact with a birthday today:
    - If they have a phone: queues a bday_wish with requires_confirmation (WhatsApp)
    - If they have email only: queues a bday_wish with requires_confirmation (email)
    - Sends admin a preview with Approve/Skip inline keyboard

    Skips contacts with neither phone nor email.
    """
    import json as _json
    import uuid as _uuid
    from aaka_queue.queue import write_item, update_status, write_outbox, set_pending_confirm

    contacts_dir = aaka_config.DATA_DIR / "contacts"
    full_json = contacts_dir / "birthdays.json"
    if not full_json.exists():
        print("[skip] No birthdays.json available")
        return

    try:
        contacts = _json.loads(full_json.read_text())
    except Exception as e:
        print(f"[skip] Could not load birthdays.json: {e}")
        return

    today = datetime.date.today()
    today_md = today.strftime("%m-%d")

    # Find admin channel for approval requests
    admin_channel = ""
    for m in aaka_config.members():
        if m.get("role") in ("owner", "admin") or m.get("admin"):
            tg = m.get("telegram")
            if tg:
                admin_channel = str(tg)
                admin_member_id = m.get("id", "")
                break

    if not admin_channel:
        print("[skip] No admin Telegram channel found")
        return

    queued = 0
    for contact in contacts:
        bday = contact.get("birthday", "")
        if len(bday) < 5:
            continue
        # birthday stored as YYYY-MM-DD or MM-DD
        contact_md = bday[5:] if len(bday) == 10 else bday
        if contact_md != today_md:
            continue

        name = contact.get("name", "")
        first = contact.get("first_name") or name.split()[0] if name else "Friend"
        phone = contact.get("mobile", "")
        email = contact.get("email", "")

        if not phone and not email:
            # Reminder only
            if not dry_run:
                write_outbox(
                    channel_id=admin_channel,
                    sender="scheduled",
                    text=f"🎂 Today is *{name}*'s birthday — no contact info on record.",
                    source="telegram",
                    ttl_minutes=120,
                )
            else:
                print(f"[dry-run] reminder only: {name}")
            queued += 1
            continue

        wish_msg = f"Happy Birthday {first}! 🎂"
        channel_label = "WhatsApp" if phone else "email"
        addr_label = phone if phone else email
        preview = (
            f"🎂 Today is *{name}*'s birthday!\n\n"
            f"Channel: {channel_label} ({addr_label})\n"
            f"Message: _{wish_msg}_\n\n"
            f"Approve to send?"
        )
        markup = {"inline_keyboard": [[
            {"text": "Send ✅", "callback_data": "yes"},
            {"text": "Skip ❌", "callback_data": "cancel"},
        ]]}

        if dry_run:
            print(f"[dry-run] bday_wish preview → {admin_channel}: {name} via {channel_label}")
            queued += 1
            continue

        # Write queue item (awaiting_confirm — executor sends after approval)
        item_id = write_item(
            intent="bday_wish",
            raw_message=f"bday wish auto {name}",
            sender=admin_channel,
            channel_id=admin_channel,
            source="scheduled",
            payload={
                "name": name,
                "first_name": first,
                "phone": phone,
                "email": email,
                "message": wish_msg,
                "from_member_id": admin_member_id,
                "channel_id": admin_channel,
                "sender": admin_channel,
                "source": "scheduled",
            },
        )
        update_status(item_id, "awaiting_confirm")
        write_outbox(
            channel_id=admin_channel,
            sender="scheduled",
            text=preview,
            source="telegram",
            ttl_minutes=360,
            reply_markup=markup,
        )
        set_pending_confirm(admin_channel, item_id)
        queued += 1

    print(f"[ok] Birthday auto-send: {queued} contact(s) processed")


def send_daily_tomorrow(dry_run: bool = False) -> None:
    """Daily 21:00: tomorrow's summary."""
    group_id = _telegram_group_id()
    content = _format_tomorrow_events()
    if not content:
        print("[skip] No events for tomorrow")
        return

    tomorrow = (datetime.date.today() + datetime.timedelta(days=1))
    fix_text = _fix_summary("today")  # tomorrow is "today" for the next check
    text = f"{REPLY_PREFIX}📅 Tomorrow at a glance:\n\n{content}{fix_text}"

    if group_id:
        _send_to_outbox(group_id, text, dry_run=dry_run)

    targets = _member_targets()
    for mid, (handle, channel) in targets.items():
        member_content = _format_member_tomorrow(mid) or content
        member_text = f"{REPLY_PREFIX}📅 Your tomorrow:\n\n{member_content}"
        _send_to_outbox(handle, member_text, source=channel, dry_run=dry_run)

    print(f"[ok] Daily tomorrow queued (group={'yes' if group_id else 'no'}, members={len(targets)})")


# ── Time-gated check ─────────────────────────────────────────────────────────

def check_and_run(dry_run: bool = False) -> None:
    """Run the appropriate summary based on current time in configured timezone."""
    now = _now_local()
    weekday = now.weekday()  # 0=Mon, 5=Sat, 6=Sun
    hour = now.hour

    print(f"[check] {now.strftime('%A %H:%M %Z')} (weekday={weekday}, hour={hour})")

    if hour == 7:   # Daily 07:00 — combined morning briefing + birthday auto-send
        send_daily_morning(dry_run=dry_run)
        send_birthday_auto(dry_run=dry_run)

    if weekday == 5 and hour == 17:  # Saturday 17:00
        send_saturday_heads_up(dry_run=dry_run)
    elif weekday == 6 and hour == 19:  # Sunday 19:00
        send_sunday_overview(dry_run=dry_run)

    if hour == 21:  # Daily 21:00
        send_daily_tomorrow(dry_run=dry_run)

    if hour not in (7, 21) and not (weekday == 5 and hour == 17) and not (weekday == 6 and hour == 19):
        print("[skip] Not a scheduled time")


def main():
    parser = argparse.ArgumentParser(description="Aaka scheduled calendar summaries")
    parser.add_argument("--check",    action="store_true", help="Gate on current time, run if due")
    parser.add_argument("--saturday", action="store_true", help="Force Saturday heads-up")
    parser.add_argument("--sunday",   action="store_true", help="Force Sunday overview")
    parser.add_argument("--tomorrow", action="store_true", help="Force daily tomorrow summary")
    parser.add_argument("--morning",    action="store_true", help="Force daily morning briefing (07a)")
    parser.add_argument("--birthday",   action="store_true", help="Force birthday-only DM (legacy)")
    parser.add_argument("--bday-auto",  action="store_true", help="Force birthday auto-send confirmations")
    parser.add_argument("--dry-run",  action="store_true", help="Print messages without sending")
    args = parser.parse_args()

    if args.check:
        check_and_run(dry_run=args.dry_run)
    elif args.saturday:
        send_saturday_heads_up(dry_run=args.dry_run)
    elif args.sunday:
        send_sunday_overview(dry_run=args.dry_run)
    elif args.tomorrow:
        send_daily_tomorrow(dry_run=args.dry_run)
    elif args.morning:
        send_daily_morning(dry_run=args.dry_run)
    elif args.birthday:
        send_birthday_morning(dry_run=args.dry_run)
    elif args.bday_auto:
        send_birthday_auto(dry_run=args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
