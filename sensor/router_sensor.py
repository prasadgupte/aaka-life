#!/usr/bin/env python3
"""
Aaka — Away / Sensor Router (VPS / always-on node)

Handles intent triage and LLM extraction. Writes side-effect-free items to the
SQLite queue. Home (executor, local Mac) polls the queue and performs Google
Calendar writes and final user confirmations.

No imports of gog.py or tasks.py at module level — enforced by design.
"""

import json
import os
import re
import sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent
)
sys.path.insert(0, str(BASE))

import logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

from llm import call_llm
import aaka_config

# Queue API
sys.path.insert(0, str(BASE))
from aaka_queue.queue import (
    write_item,
    is_duplicate,
    read_pending,
    set_pending_confirm,
    get_pending_confirm,
    clear_pending_confirm,
    update_status,
    update_payload,
    get_item,
    read_pending_outbox,
    mark_outbox_sent,
    get_active_reply_request,
    record_agent_reply,
)

from aaka_config import reply_prefix
REPLY_PREFIX = reply_prefix()

# ── VPS scoped token for direct calendar + task writes ────────────────────────
# Set at startup using AAKA_CONFIG_DIR so it resolves correctly inside Docker.
_VPS_TOKEN = Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "tokens" / "token_vps.json"


def _exec_cal_direct_vps(intent: str, payload: dict) -> str:
    """Execute a calendar intent directly on the sensor using token_vps.json.

    Returns a short confirmation string on success; raises on failure
    (caller falls through to queue path).
    """
    import re as _re
    if intent == "add_event":
        from skills.calendar.add_event import write_event as _write_event
        result = _write_event(payload, token_file=_VPS_TOKEN)
        count = result.get("count", 1)
        title = payload.get("title", "?")
        word = "event" if count == 1 else f"{count} events"
        return f"✅ Added {word}: {title}"
    elif intent == "add_event_batch":
        from skills.calendar.add_event import write_event as _write_event
        events = payload.get("events", [])
        total = 0
        for ev in events:
            r = _write_event(ev, token_file=_VPS_TOKEN)
            total += r.get("count", 0)
        word = "event" if total == 1 else f"{total} events"
        return f"✅ Added {word}"
    elif intent == "block_cal":
        from skills.calendar.gog import create_event as _gog_create
        blocks = payload.get("blocks", [])
        tz_str = aaka_config.timezone()
        for b in blocks:
            _gog_create(title=b["title"], start=b["start"], end=b["end"],
                        timezone=tz_str, token_file=_VPS_TOKEN)
        recap = payload.get("recap", "")
        msg = f"✅ Added {len(blocks)} block(s)."
        return f"{msg}\n{recap}".rstrip() if recap else msg
    else:
        raise ValueError(f"No VPS direct path for intent: {intent}")

# Comma-separated allowlist; empty = all intents enabled
_ENABLED_INTENTS: set[str] = set(os.environ.get("SENSOR_ENABLED_INTENTS", "").split(",")) - {""}

# ── Intent patterns and local dispatch (moved to sensor/intent_registry.py) ───
from sensor.intent_registry import match_intent, dispatch_local as _dispatch_local

# Kept for backward compatibility (scheduled_summaries.py imports match_intent)
_INTENT_PATTERNS = []  # no longer the canonical source; see intent_registry.py

# Intents the Sensor can fully resolve itself (no queue write needed)
_LOCAL_INTENTS = {"today_schedule", "weekly_schedule", "health_check", "menu", "members_list", "list_tags", "tag_manage", "list_tasks", "snooze_task", "edit_task", "delete_task", "llm_call", "buy_list", "birthday_list", "read_note", "engage_report", "pdf_tool", "approve_sender", "deny_sender"}
# Intents that need Executor involvement
_QUEUE_INTENTS = {"add_event", "add_event_batch", "fix_event", "edit_event", "route_tags", "block_cal", "drop_file", "file_sync", "linkedin_post", "mail_fetch", "mail_view", "pw_manage", "confirm_approval", "keep_original"}

# ── Message parsing ──────────────────────────────────────────────────────────
#
# OpenClaw passes messages to the CLI backend in several formats:
#
# Format A — Telegram DM/group (structured metadata):
#   System: [timestamp] WhatsApp gateway connected...
#   [media attached: /path/file.jpg (image/jpeg) | /path/file.jpg]   ← optional
#   ...instructions...                                                ← optional
#   Conversation info (untrusted metadata):
#   ```json
#   {"chat_id": "telegram:123456789", "message_id": "358", "sender_id": "123456789", ...}
#   ```
#   Sender (untrusted metadata):
#   ```json
#   {"label": "P G (123456789)", "id": "123456789", ...}
#   ```
#   /menu                                                             ← actual message
#   /tmp/openclaw/.../image.jpg                                       ← optional media path
#
# Format B — WhatsApp inline header:
#   [WhatsApp +10000000000 +18m Fri 2026-04-24 18:24 UTC] (self): [openclaw] /menu
#
# Format C — plain text (dry-run, HTTP serve):
#   /menu
#
# All format parsing is handled by gateway/ingress.py::normalize().


def match_intent(message: str) -> "str | None":
    # Delegates to intent_registry (imported at top of file)
    from sensor.intent_registry import match_intent as _ri_match
    return _ri_match(message.lower())


# ── Confirm / cancel handling ─────────────────────────────────────────────────

def _is_confirm(m: str) -> bool:
    return bool(re.match(r'^(ok|yes|save|confirm|commit|force)\b', m)) or m.startswith('👍')

def _is_cancel(m: str) -> bool:
    return bool(re.match(r'^(cancel|no|abort|discard)\b', m)) or m.startswith('👎')


def _parse_confirm_modifiers(msg: str, payload: dict) -> "tuple[dict, str]":
    """Parse modifier words from a confirmation message.
    Returns (mods_dict, human_feedback_string). Empty dict if no modifiers."""
    from skills.calendar.prepare_event import parse_pad_modifier
    mods: dict = {}
    feedback_parts: list[str] = []

    # Strip leading confirm word if present
    text = re.sub(r'^(ok|yes|save|confirm|commit)\s*', '', msg.strip(), flags=re.I)

    # pad modifier
    pad = parse_pad_modifier(text)
    if pad:
        mods["pad"] = pad
        feedback_parts.append(f"Padding: {pad[0]}min before, {pad[1]}min after")

    # carrier <name>
    m = re.search(r'\bcarrier\s+(\w+)', text, re.I)
    if m:
        mods["carrier"] = m.group(1).capitalize()
        feedback_parts.append(f"Carrier: {mods['carrier']}")

    # private
    if re.search(r'\bprivate\b', text, re.I):
        mods["private"] = True
        feedback_parts.append("Private event")

    # no-email
    if re.search(r'\bno[-\s]?email\b', text, re.I):
        mods["no_email"] = True
        feedback_parts.append("No email invites")

    # !MemberName — add to conflict_members for re-check
    extra_members = re.findall(r'!([A-Z][a-z]+)', msg)
    if extra_members:
        resolved = []
        for name in extra_members:
            member = aaka_config.member_by_name(name)
            if member:
                resolved.append(member["id"])
        if resolved:
            existing = payload.get("conflict_members", [])
            merged = list({*existing, *resolved})
            mods["conflict_members"] = merged
            feedback_parts.append("Also checking: " + ", ".join(resolved))

    return (mods, "  ·  ".join(feedback_parts))


def _apply_confirm_modifiers(payload: dict, mods: dict) -> dict:
    """Apply parsed modifiers to a payload dict. Returns updated payload."""
    from skills.calendar.prepare_event import apply_event_padding
    from skills.calendar.add_event import build_title

    if "pad" in mods:
        before, after = mods["pad"]
        payload = apply_event_padding(payload, before, after)

    if "carrier" in mods:
        payload["carrier"] = mods["carrier"]
        payload["title"] = build_title(
            payload.get("attendee") or aaka_config.group_name(),
            payload.get("type", "other"),
            payload.get("summary", "Event"),
            carrier_override=mods["carrier"],
        )

    if "private" in mods:
        payload["private"] = True

    if "no_email" in mods:
        payload["no_email"] = True
        payload["guests"] = []

    if "conflict_members" in mods:
        payload["conflict_members"] = mods["conflict_members"]

    return payload


# ── Per-intent extraction helpers ─────────────────────────────────────────────

def _extract_event(message: str, sender_email: str = "", sender_member_id: str = "") -> dict:
    """Call prepare_event extraction (LLM) and return payload dict."""
    from skills.calendar.prepare_event import extract_events
    return extract_events(message, sender_email=sender_email, sender_member_id=sender_member_id)


def _extract_events_batch(message: str, sender_email: str = "", sender_member_id: str = "") -> "list[dict]":
    """Extract one or more events from natural language. Always returns a list."""
    from skills.calendar.prepare_event import extract_batch
    return extract_batch(message, sender_email=sender_email, sender_member_id=sender_member_id)


def _format_event_preview(payload: dict) -> str:
    from skills.calendar.prepare_event import format_review
    return format_review(payload)


def _extract_task(message: str, sender_member_id: str = "") -> dict:
    from skills.tasks.prepare_task import prepare_task
    text = re.sub(r'^/(addtask|task)\s*', '', message, flags=re.I).strip()
    return prepare_task(text, sender_member_id=sender_member_id)


def _format_task_preview(payload: dict) -> str:
    title = payload.get("title", "(unknown)")
    due = payload.get("due_date", "")
    due_str = f"  📅 Due: {due}" if due else ""
    urgent = "🔴 " if payload.get("urgent") else ""
    starred = "⭐ " if payload.get("starred") else ""
    tags = " ".join(payload.get("tags", []))
    tags_str = f"  {tags}" if tags else ""
    return f"📝 New task: {urgent}{starred}*{title}*{due_str}{tags_str}\n\nConfirm adding? Reply: yes / cancel"


def _parse_edit_date(text: str) -> str:
    """Parse a date from /edit change text. Returns ISO date string or ''."""
    import datetime as _dt
    today = _dt.date.today()
    t = text.lower().strip()
    if t == "today":
        return today.isoformat()
    if t == "tomorrow":
        return (today + _dt.timedelta(days=1)).isoformat()
    # Day names: monday, tuesday, ...
    days_of_week = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if t in days_of_week:
        target_dow = days_of_week.index(t)
        current_dow = today.weekday()
        diff = (target_dow - current_dow) % 7
        if diff == 0:
            diff = 7  # next occurrence
        return (today + _dt.timedelta(days=diff)).isoformat()
    # "next week" = next Monday
    if t == "next week":
        diff = (7 - today.weekday()) % 7 or 7
        return (today + _dt.timedelta(days=diff)).isoformat()
    # "Nd" or "Nw" shorthand
    m = re.match(r'^(\d+)\s*([dw])$', t)
    if m:
        n = int(m.group(1))
        days = n * 7 if m.group(2) == 'w' else n
        return (today + _dt.timedelta(days=days)).isoformat()
    # ISO date: 2026-05-10
    if re.match(r'^\d{4}-\d{2}-\d{2}$', t):
        return t
    # "no date" / "none" — remove due date
    if t in ("none", "no date", "clear"):
        return ""
    return ""


def _extract_complete_task(message: str) -> dict:
    """Extract task reference from a /done or /complete message.

    Handles:
      /done 3          → {"task_nums": [3], "title_hint": ""}
      /done 1 3 5      → {"task_nums": [1, 3, 5], "title_hint": ""}
      /done today      → {"history": "today", "task_nums": [], "title_hint": ""}
      /done week       → {"history": "week", "task_nums": [], "title_hint": ""}
      /done buy milk   → {"task_nums": [], "title_hint": "buy milk"}
    """
    text = re.sub(r'^/(done|complete)\s*', '', message.strip(), flags=re.I).strip()
    # History keywords
    if text.lower() in ("today", "week", "yesterday"):
        return {"history": text.lower(), "task_nums": [], "title_hint": ""}
    # Multiple numbers: "1 3 5"
    nums = re.findall(r'\d+', text)
    if nums and re.match(r'^[\d\s,]+$', text):
        return {"task_nums": [int(n) for n in nums], "title_hint": ""}
    # Single number
    if nums and len(nums) == 1 and re.match(r'^\d+\s*$', text):
        return {"task_nums": [int(nums[0])], "title_hint": ""}
    return {"task_nums": [], "title_hint": text}


def _resolve_complete_task_payload(payload: dict) -> dict:
    """Resolve task_num(s) → task_id(s) using local tasks order file."""
    from skills.tasks.local_tasks import title_for_num, _order_file
    import json as _json
    task_nums = payload.get("task_nums", [])
    # Single number (backward compat)
    if len(task_nums) == 1:
        task_num = task_nums[0]
        order_file = _order_file()
        if order_file.exists():
            try:
                ids = _json.loads(order_file.read_text())
                idx = task_num - 1
                if 0 <= idx < len(ids):
                    return {"task_id": ids[idx], "title_hint": title_for_num(task_num), "task_nums": task_nums}
            except Exception:
                pass
    # Multiple numbers
    if len(task_nums) > 1:
        return {"task_id": "", "title_hint": "", "task_nums": task_nums, "bulk": True}
    # Title-hint path: search local tasks by keyword
    hint = payload.get("title_hint", "")
    if hint:
        from skills.tasks.local_tasks import find_by_title
        t = find_by_title(hint)
        if t:
            return {"task_id": t["id"], "title_hint": t["title"], "task_nums": []}
    return {"task_id": "", "title_hint": hint, "task_nums": task_nums}


def _format_complete_task_preview(payload: dict) -> str:
    resolved = _resolve_complete_task_payload(payload)
    name = resolved.get("title_hint") or f"task #{payload.get('task_num', '?')}"
    return f"✅ Mark done: *{name}*?\n\nReply: yes / cancel"


# ── Vault routing helpers ──────────────────────────────────────────────────────

def _is_allowed_channel(sender_id: str, channel_id: str) -> bool:
    """
    Allow if sender is a known family member (DM) OR channel matches a known group.
    Everything else is silently dropped — no reply sent.
    """
    if aaka_config.member_by_sender(sender_id):
        return True
    allowed_groups = {g.strip() for g in [
        os.environ.get("WHATSAPP_GROUP_JID", ""),
        os.environ.get("TELEGRAM_GROUP_ID", ""),
    ] if g.strip()}
    return bool(allowed_groups & {sender_id, channel_id})


def _format_members() -> str:
    """Format the full member roster with rights for the /members admin command."""
    _ROLE_EMOJI = {"adult": "👤", "child": "🧒", "grandparent": "👴", "bot": "🤖"}
    lines = ["👥 *Members*\n"]
    for m in aaka_config.members():
        name = m.get("name", m.get("id", "?"))
        nick = m.get("nick", "")
        role = m.get("role", "")
        label = nick if nick else role
        role_icon = _ROLE_EMOJI.get(role, "")
        role_tag = f"{label} {role_icon}".strip()

        header = f"*{name}*  {role_tag}"
        if m.get("admin"):
            header += "  🛡️ admin"
        lines.append(header)

        # Contact channels
        channels = []
        if m.get("whatsapp"):
            channels.append("WhatsApp")
        if m.get("telegram"):
            channels.append("Telegram")
        if m.get("email"):
            channels.append(m["email"])
        if channels:
            lines.append(f"  📡 {' · '.join(channels)}")

        # Rights
        rights = []
        if not m.get("shared_access", True):
            rights.append("📋 shared 🔒")
        auth = m.get("auth", {})
        if auth:
            scopes = auth.get("scopes", [])
            if any("calendar" in s for s in scopes):
                rights.append("📅 cal token")
            if any("tasks" in s for s in scopes):
                rights.append("✅ tasks token")
        if m.get("invite_as_guest"):
            rights.append("📩 gets invites")
        if m.get("needs_carrier"):
            rights.append("🚗 needs carrier")
        if rights:
            lines.append(f"  {' · '.join(rights)}")

        lines.append("")

    return "\n".join(lines).rstrip()


def _namespace_for_sender(sender: str) -> str | None:
    """Map sender ID → member id (e.g. 'alice'). Returns None if unrecognised."""
    m = aaka_config.member_by_sender(sender)
    return m["id"] if m and m.get("id") else None


def _parse_member_arg(message: str) -> str | None:
    """Extract optional member name from 't @tsu' or '/today alice' → member id, or None."""
    # @name syntax (canonical form); skip meta-tokens handled elsewhere
    m = re.search(r'@(\w+)', message, re.IGNORECASE)
    if m:
        name = m.group(1).lower()
        if name not in ("me", "all", "fam"):
            member = aaka_config.member_by_name(name)
            return member["id"] if member else None
    # Positional fallback: "/today alice"
    m = re.search(r'(?:today|week)\s+(\w+)', message, re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).lower()
    member = aaka_config.member_by_name(name)
    return member["id"] if member else None


def _strip_me_flag(message: str) -> tuple[str, bool]:
    """Remove '@me' or '#me' from message. Returns (cleaned_message, had_flag)."""
    cleaned = re.sub(r'(?i)\s*[@#]me\b', '', message).strip()
    return cleaned, cleaned != message


def _strip_fix_flag(message: str) -> tuple[str, bool]:
    """Remove '#fix' from message. Returns (cleaned_message, had_flag)."""
    cleaned = re.sub(r'(?i)\s*#fix\b', '', message).strip()
    return cleaned, cleaned != message


# ── /status code + queue handlers (shared with admin CLI) ────────────────────

from skills.status.status_core import status_code as _handle_status_code, status_queue as _handle_status_queue


def _handle_status_cron() -> str:
    """Show all VPS cron jobs and their last recorded run."""
    import datetime as _dt

    LOGS = aaka_config.LOGS_DIR

    def _last_line(log_file: str) -> str:
        p = LOGS / log_file
        if not p.exists():
            return "(no log)"
        try:
            lines = [l for l in p.read_text().splitlines() if l.strip()]
            return lines[-1][:120] if lines else "(empty)"
        except Exception:
            return "(error)"

    def _log_age(log_file: str) -> str:
        """Return human-readable age of last log write."""
        import os
        p = LOGS / log_file
        if not p.exists():
            return "never"
        try:
            secs = int(_dt.datetime.now().timestamp() - os.path.getmtime(p))
            if secs < 120:    return f"{secs}s ago"
            if secs < 3600:   return f"{secs//60}m ago"
            if secs < 86400:  return f"{secs//3600}h ago"
            return f"{secs//86400}d ago"
        except Exception:
            return "?"

    jobs = [
        ("flush_outbox",  "*/3 min",  "outbox_flush.log"),
        ("summaries",     "hourly",   "summaries.log"),
        ("sidecar_sync",  "*/30 min", "sidecar_sync.log"),
        ("token_expiry",  "23:00",    "token_expiry.log"),
    ]

    lines = ["⏰ Cron jobs:"]
    for name, schedule, logfile in jobs:
        age  = _log_age(logfile)
        last = _last_line(logfile)
        lines.append(f"\n{name} ({schedule}) — {age}")
        lines.append(f"  {last}")

    return "\n".join(lines)


# ── Voice context helpers (zero-token content scan) ───────────────────────────

def _analyze_today_content(content: str) -> dict:
    """Scan today.md text for event count and carrier presence. Zero LLM."""
    carrier_names = {n.lower() for n in aaka_config.needs_carrier()}
    lines = content.lower()
    event_count = content.count("\n- ") + content.count("\n• ")
    carrier_kws = ["🤝", "takes", "drives", "pickup", "dropoff", "drops", "carrier"]
    has_carrier = (
        any(n in lines for n in carrier_names)
        and any(k in lines for k in carrier_kws)
    )
    child, child_time = "", ""
    if has_carrier:
        for name in carrier_names:
            if name in lines:
                child = name.capitalize()
                m = re.search(rf"{re.escape(name)}.{{0,40}}(\d{{1,2}}:\d{{2}})", lines)
                child_time = m.group(1) if m else ""
                break
    return {"event_count": event_count, "has_carrier": has_carrier,
            "child": child, "child_time": child_time}


def _analyze_weekly_content(content: str) -> dict:
    """Scan weekly.md text for total event count. Zero LLM."""
    return {"weekly_count": content.count("\n- ") + content.count("\n• ")}


# ── Note sync helpers ─────────────────────────────────────────────────────────

def _update_sync_marker(topic: str, note_path: "Path") -> int:
    """Move '--- synced ---' marker to reflect completed file_upsert items.

    Returns synced_through count (number of entries synced), or 0 if none.
    Rewrites the file in-place only when the marker position changes.
    """
    import sqlite3 as _sq
    import json as _json

    MARKER = "--- synced ---"
    db_path = os.environ.get("QUEUE_DB", str(
        Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "data" / "queue" / "butler.db"
    ))

    synced_through = 0
    try:
        conn = _sq.connect(db_path)
        conn.execute("PRAGMA busy_timeout=3000")
        row = conn.execute(
            "SELECT MAX(CAST(json_extract(result, '$.synced_through') AS INTEGER))"
            " FROM queue_items WHERE intent='file_upsert' AND status='done'"
            " AND json_extract(payload, '$.topic') = ?",
            (topic,),
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            synced_through = int(row[0])
    except Exception:
        return 0

    if synced_through == 0:
        return 0

    content = note_path.read_text()
    lines = content.splitlines()

    # Locate end of YAML frontmatter
    fm_end = -1
    if lines and lines[0].strip() == "---":
        for i, l in enumerate(lines[1:], 1):
            if l.strip() == "---":
                fm_end = i
                break

    frontmatter = lines[:fm_end + 1] if fm_end >= 0 else []
    body_lines = lines[fm_end + 1:] if fm_end >= 0 else lines

    # Remove existing marker
    body_lines = [l for l in body_lines if l.strip() != MARKER]

    # Insert marker after synced_through entry lines
    entry_count = 0
    new_body: list = []
    marker_inserted = False
    for l in body_lines:
        if l.startswith("- "):
            entry_count += 1
            new_body.append(l)
            if entry_count == synced_through and not marker_inserted:
                new_body.append(MARKER)
                marker_inserted = True
        else:
            new_body.append(l)

    if not marker_inserted:
        new_body.append(MARKER)

    new_content = "\n".join(frontmatter + new_body)
    if not new_content.endswith("\n"):
        new_content += "\n"
    note_path.write_text(new_content)
    return synced_through


def _queue_file_sync(list_name: str, sender: str, channel_id: str, source: str = "") -> None:
    """Queue a file_sync item for a list file. Delegates to sensor.helpers."""
    from sensor.helpers import queue_file_sync as _qfs
    _qfs(list_name, sender, channel_id, source)


def _queue_note_sync(source_path: str, dest_path: str, sender: str, channel_id: str) -> None:
    """Queue a file_sync item for an arbitrary note path. Delegates to sensor.helpers."""
    from sensor.helpers import queue_note_sync as _qns
    _qns(source_path, dest_path, sender, channel_id)


# ── Fix detail card ───────────────────────────────────────────────────────────

def _format_fix_detail(item: dict) -> str:
    """Compact event detail card for 'c fix N' (no carrier name given)."""
    import datetime as _dt
    from skills.calendar.fix_analyzer import lookup_weekly_event

    def _dur(start: str, end: str) -> str:
        try:
            s = _dt.datetime.strptime(start, "%H:%M")
            e = _dt.datetime.strptime(end, "%H:%M")
            mins = int((e - s).total_seconds() / 60)
            return f"({mins}m)" if mins < 60 else (f"({mins // 60}h{mins % 60}m)" if mins % 60 else f"({mins // 60}h)")
        except Exception:
            return ""

    def _date_fmt(date_str: str) -> str:
        try:
            return _dt.date.fromisoformat(date_str).strftime("%a %d %b")
        except Exception:
            return date_str

    def _event_block(ev_ref: dict, full: dict | None) -> list[str]:
        """Render one event as 2-3 lines: icon+title+time, attendees+location, description."""
        title = ev_ref.get("title", "(no title)")
        start = ev_ref.get("start", "")
        end   = ev_ref.get("end", "")
        emoji = (full or {}).get("emoji", "📅")
        dur   = _dur(start, end) if start and end else ""
        block = [f"{emoji} {title} {start} {dur}".strip()]

        # Attendees + location on one line
        extras = []
        if full:
            rs       = full.get("response_status", {}) or {}
            att_names = full.get("attendee_names", {}) or {}
            att_parts = []
            for email, status in rs.items():
                name = att_names.get(email) or aaka_config.name_by_email(email) or email
                icon = "✓" if status == "accepted" else "👻" if status in ("needsAction", "tentative") else "✗"
                att_parts.append(f"{name} ({icon})")
            if att_parts:
                extras.append("👥 " + ", ".join(att_parts))
            loc = (full.get("location") or "").strip()
            if loc:
                extras.append(f"📍 {loc}")
        if extras:
            block.append("  ".join(extras))

        # Description
        if full:
            desc = (full.get("description") or "").strip()
            if desc:
                block.append(f"📝 {desc[:100]}{'…' if len(desc) > 100 else ''}")

        return block

    date_fmt = _date_fmt(item.get("date", ""))
    lines = [date_fmt] if date_fmt else []

    if item.get("type") == "conflict":
        ea_ref = item.get("event_a", {})
        eb_ref = item.get("event_b", {})
        ea_full = lookup_weekly_event(ea_ref.get("event_id", ""))
        eb_full = lookup_weekly_event(eb_ref.get("event_id", ""))
        lines += _event_block(ea_ref, ea_full)
        overlap = item.get("overlap_start", "")
        sep = f"────🧱 ~{overlap}────" if overlap else "────🧱────"
        lines.append(sep)
        lines += _event_block(eb_ref, eb_full)
    else:
        # Non-conflict: single event card
        full = lookup_weekly_event(item.get("event_id", ""))
        lines += _event_block(
            {"title": item.get("title", ""), "start": item.get("start", ""), "end": item.get("end", "")},
            full,
        )

    return "\n".join(lines)


# ── Today schedule handler (extracted for reuse by scheduled_summaries) ──────

def _build_today_schedule(mid: str, sender: str = "", *, include_tasks: bool = True) -> str:
    """Build today's schedule for a member. Returns formatted text.

    Used by both the interactive /t handler and the 07:00 scheduled push.
    Includes: voiced calendar, compact task counts (with tap-to-copy hints),
    today's birthdays with direct wish links, and a fix summary.

    The morning push opts out of the compact task counts (include_tasks=False)
    because it appends its own richer summary_for_push() block after the
    schedule — keep them mutually exclusive to avoid duplication.
    """
    import urllib.parse
    CALENDAR = aaka_config.CALENDAR_DIR

    path = CALENDAR / f"today_{mid}.md"
    if not path.exists():
        path = CALENDAR / "today.md"
    if not path.exists():
        return "📅 No calendar data. Sync pending."

    raw = path.read_text()

    # ── Inline fix merge (conflicts, carriers, unaccepted) ─────────────
    try:
        from skills.calendar.fix_analyzer import analyze_fix, merge_fixes_inline, save_fix_list
        issues = analyze_fix("today", member_id=mid)
        raw = merge_fixes_inline(raw, issues)
        if sender and issues:
            save_fix_list(sender, issues)
    except Exception:
        pass

    from skills.voice import get_voice
    m_obj = aaka_config.member_by_name(mid) or {}
    name = m_obj.get("name", mid.capitalize())
    ctx = {"name": name, **_analyze_today_content(raw)}
    reply = get_voice().apply("today_schedule", raw, ctx)

    # ── Task counts (compact — pokes /tasks for details) ─────────────────
    if include_tasks:
        try:
            from skills.tasks.local_tasks import list_open
            from skills.tasks.format import render_task_counts
            _task_counts = render_task_counts(list_open(), period="today")
            if _task_counts:
                reply += "\n\n" + _task_counts
        except Exception:
            pass

    # ── Today's birthdays — same renderer as /bday ──────────────────────
    try:
        from skills.contacts.birthday_list import (
            query as _bday_query, _load_window, _load_full, _md_to_date,
        )
        from skills.contacts.format import render_today_birthdays
        from datetime import date as _date
        _today = _date.today()
        _window = _load_window()
        _bday_today = [e for e in _window
                       if _md_to_date(e.get("month_day", "00-01")) == _today]
        if _bday_today:
            _full_map = {e["name"]: e for e in _load_full()}
            bday_block = render_today_birthdays(_bday_today, _full_map)
            if bday_block:
                reply += "\n\n" + bday_block

        # Seed bday list for "bday N" resolution
        if sender:
            _bday_query("", sender_id=sender)
    except Exception:
        pass

    return reply


# ── Read-only intent handlers (Sensor resolves these directly) ────────────────

def _handle_local_intent(intent: str, message: str = "", sender: str = "", channel_id: str = "", source: str = "") -> str:
    """Dispatch a locally-handled intent via sensor/intent_registry.py domain modules."""
    result = _dispatch_local(intent, message, sender, channel_id, source)
    if result is not None:
        return result
    return "❓ Unknown intent."


def _handle_invite(message: str, sender_id: str) -> str:
    """Admin-only: `/invite <name>` → mint a one-time code + a wa.me deep link.
    The invitee taps it, sends the pre-filled message, and is auto-registered on
    their first message (see sensor/wa_onboard.try_signup)."""
    requester = aaka_config.member_by_sender(sender_id)
    if not requester or not aaka_config.member_is_admin(requester.get("id", "")):
        return "🔒 Only an admin can invite members."
    arg = re.sub(r"^/invite\b", "", message, flags=re.I).strip()
    if not arg:
        return "Usage: `/invite <name>` — e.g. `/invite Sam` (must be an existing member)."
    # Resolve the target member by id or name (case-insensitive); create one on
    # the fly if unknown — no aaka.yaml editing (nobody opens config files).
    a = arg.lower()
    target = next((m for m in aaka_config.members()
                   if m.get("id", "").lower() == a or m.get("name", "").lower() == a), None)
    created = False
    if not target:
        target = aaka_config.add_dynamic_member(arg)
        created = True
    from sensor import wa_onboard
    code = wa_onboard.create_invite(target["id"], target.get("name", arg))
    url, text = wa_onboard.invite_link(code, target.get("name", arg))
    made = f"👤 Added *{target.get('name', arg)}* as a new member.\n" if created else ""
    if url:
        return (f"{made}✅ Invite ready. Send them this link:\n\n"
                f"{url}\n\n"
                f"They tap it, hit send, and I'll welcome them automatically. Code `{code}` (valid 7 days).")
    return (f"{made}✅ Invite code for {target.get('name', arg)}: `{code}` (valid 7 days).\n\n"
            f"Ask them to WhatsApp me: “{text}”. (Couldn't build a wa.me link — "
            f"WhatsApp session not connected, so I don't know my own number yet.)")


def _handle_tools(message: str, sender_id: str) -> str:
    """Admin-only: `/tools` lists registered aaka Tools; `/tools run <name>` runs
    one on demand and reports the result inline. See docs/aaka-tools.md."""
    requester = aaka_config.member_by_sender(sender_id)
    if not requester or not aaka_config.member_is_admin(requester.get("id", "")):
        return "🔒 Only an admin can manage tools."
    from sensor import tool_runner
    arg = re.sub(r"^/tools\b", "", message, flags=re.I).strip()
    m = re.match(r"run\s+(\S+)\s*(.*)$", arg, re.I)
    if m:
        name, extra = m.group(1), m.group(2).strip()
        res = tool_runner.run_and_report(name, extra)
        head = "✅" if res.get("ok") else "⚠️"
        return f"{head} {name}: {res.get('summary') or res.get('error') or 'done'}"
    rows = tool_runner.status()
    if not rows:
        return ("🧰 No tools registered yet. Add one via MCP (`register_tool`) or "
                "config/tools.yaml. See docs/aaka-tools.md.")
    lines = ["🧰 *aaka Tools*"]
    for r in rows:
        dot = "🟢" if r["enabled"] else "⚪️"
        sched = f" · `{r['schedule']}`" if r["schedule"] else " · on-demand"
        last = ""
        if r["last_run"]:
            last = f" · last {'✓' if r['last_ok'] else '✗'} {r['last_run'][11:16]}"
        cmd = f" · {r['command']}" if r["command"] else ""
        lines.append(f"{dot} *{r['name']}* ({r['placement']}){sched}{cmd}{last}")
    lines.append("\n`/tools run <name>` to run now.")
    return "\n".join(lines)


_IBAN_LEN = {
    "AL": 28, "AD": 24, "AT": 20, "AZ": 28, "BH": 22, "BE": 16, "BA": 20,
    "BR": 29, "BG": 22, "CR": 22, "HR": 21, "CY": 28, "CZ": 24, "DK": 18,
    "DO": 28, "EE": 20, "FI": 18, "FR": 27, "GE": 22, "DE": 22, "GI": 23,
    "GR": 27, "GT": 28, "HU": 28, "IS": 26, "IQ": 23, "IE": 22, "IL": 23,
    "IT": 27, "JO": 30, "KZ": 20, "XK": 20, "KW": 30, "LV": 21, "LB": 28,
    "LI": 21, "LT": 20, "LU": 20, "MT": 31, "MR": 27, "MU": 30, "MD": 24,
    "MC": 27, "ME": 22, "NL": 18, "MK": 19, "NO": 15, "PK": 24, "PS": 29,
    "PL": 28, "PT": 25, "QA": 29, "RO": 24, "LC": 32, "SM": 27, "ST": 25,
    "SA": 24, "RS": 22, "SC": 31, "SK": 24, "SI": 19, "ES": 24, "SE": 24,
    "CH": 21, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "AE": 23, "GB": 22,
    "VG": 24,
}


def _parse_pay(message: str) -> dict:
    """Parse 'pay NAME IBAN AMOUNT REFERENCE' from a free-form message.

    Returns dict with keys: name, iban, amount, reference, bic (may be empty).
    Raises ValueError with a user-friendly hint if parsing fails.
    """
    import re as _re
    text = _re.sub(r"^/pay\s*|^pay\s+", "", message.strip(), flags=_re.I).strip()

    # Find IBAN start: 2-letter country code + 2 check digits
    iban_start = _re.compile(r'\b([A-Z]{2})(\d{2})', _re.I)
    m = iban_start.search(text)
    if not m:
        raise ValueError(
            "Couldn't find an IBAN. Format:\n"
            "`pay NAME IBAN AMOUNT REFERENCE`\n"
            "e.g. `pay Max Muster DE89 3704 0044 0532 0130 00 96.50 Miete Mai`"
        )

    country = m.group(1).upper()
    iban_len = _IBAN_LEN.get(country, 34)  # default to max if unknown country

    # Collect exactly iban_len alphanumeric chars starting at the match position
    raw_from_here = text[m.start():]
    alphanum = _re.sub(r'[^A-Z0-9]', '', raw_from_here.upper())
    if len(alphanum) < iban_len:
        raise ValueError(f"IBAN too short for {country} (need {iban_len} chars).")
    iban = alphanum[:iban_len]

    # Find where the IBAN block ends in the original string
    consumed = 0
    pos = m.start()
    while consumed < iban_len and pos < len(text):
        if text[pos].upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            consumed += 1
        pos += 1
    iban_end = pos

    before = text[:m.start()].strip()
    after  = text[iban_end:].strip()

    # Amount: optional currency prefix/suffix, decimal or comma separator
    amt_pat = _re.compile(
        r'(?:EUR?|€)\s*(\d{1,9}(?:[.,]\d{1,2})?)'   # €96.50 or EUR96
        r'|(\d{1,9}(?:[.,]\d{1,2})?)\s*(?:EUR?|€)'  # 96.50€ or 96EUR
        r'|(\d{1,9}(?:[.,]\d{1,2})?)',               # bare 96.50
        _re.I,
    )

    am = amt_pat.search(after)
    if am:
        name = before.strip()
        raw_amt = am.group(1) or am.group(2) or am.group(3)
        amount = float(raw_amt.replace(",", "."))
        reference = after[am.end():].strip()
    else:
        # Fallback: amount may appear in the name portion (before IBAN), e.g. "pay Max €670 DE89..."
        am2 = amt_pat.search(before)
        if am2:
            name = before[:am2.start()].strip()
            raw_amt = am2.group(1) or am2.group(2) or am2.group(3)
            amount = float(raw_amt.replace(",", "."))
            reference = after.strip()
        else:
            raise ValueError("Amount missing. Include a number, e.g. `96.50` or `€450`.")

    if not name:
        raise ValueError("Beneficiary name missing before IBAN.")
    if not reference:
        raise ValueError("Payment reference (Verwendungszweck) missing.")

    return {"name": name, "iban": iban, "amount": amount, "reference": reference, "bic": ""}


def _handle_pay(
    message: str,
    *,
    channel_id: str,
    sender: str,
    message_id: "str | None",
    dry_run: bool,
    source: str,
) -> str:
    """Generate an EPC QR code and send it back via Telegram sendPhoto."""
    import urllib.request as _urllib_req
    import mimetypes as _mimetypes

    try:
        pay = _parse_pay(message)
    except ValueError as exc:
        return _reply(f"⚠️ {exc}", channel_id=channel_id, sender=sender,
                      message_id=message_id, dry_run=dry_run, source=source)

    if dry_run:
        return (f"[dry-run] pay  name={pay['name']}  iban={pay['iban']}"
                f"  amount=EUR{pay['amount']:.2f}  ref={pay['reference']}")

    try:
        from tools.epc_qr import generate_epc_qr as _gen_qr
        qr_path = _gen_qr(
            name=pay["name"],
            iban=pay["iban"],
            amount=pay["amount"],
            reference=pay["reference"],
            bic=pay["bic"],
            output_path=f"/tmp/epc-qr-{pay['iban'][-6:]}.png",
        )
    except Exception as exc:
        return _reply(f"⚠️ QR generation failed: {exc}", channel_id=channel_id,
                      sender=sender, message_id=message_id, dry_run=dry_run, source=source)

    # Send the PNG via egress gateway
    try:
        from gateway.egress import send as _egress_send, photo as _egress_photo
        chat_id = channel_id or sender
        caption = (f"💳 *{pay['name']}*\n"
                   f"IBAN: `{pay['iban']}`\n"
                   f"Amount: EUR {pay['amount']:.2f}\n"
                   f"Ref: {pay['reference']}")
        _egress_send(_egress_photo(
            chat_id, "telegram", qr_path.read_bytes(), "router_sensor",
            caption=caption,
            reply_to=str(message_id) if message_id else None,
        ))
    except Exception as exc:
        return _reply(f"⚠️ Couldn't send QR: {exc}", channel_id=channel_id,
                      sender=sender, message_id=message_id, dry_run=dry_run, source=source)

    # Return empty — the photo is the reply
    return ""


# ── Sender approval / denial ──────────────────────────────────────────────────

def _cancel_sender_approvals(sender_id: str) -> int:
    """Cancel awaiting_confirm queue items for an unknown sender. Returns count."""
    from aaka_queue.queue import _connect
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _connect()
    r = conn.execute(
        "UPDATE queue_items SET status='cancelled', updated_at=? "
        "WHERE status='awaiting_confirm' AND sender=?",
        (now, sender_id),
    )
    conn.commit()
    return r.rowcount


def _write_sender_list(filename: str, sender_id: str) -> None:
    """Add sender_id to a JSON allowlist/blocklist in the config data dir."""
    import json as _json
    data_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / filename
    data: dict = {}
    if path.exists():
        try:
            data = _json.loads(path.read_text())
        except Exception:
            pass
    data[sender_id] = True
    path.write_text(_json.dumps(data, indent=2))


def _handle_approve_sender(message: str, requester: str) -> str:
    """Approve an unknown sender — allows future messages, clears pending queue items."""
    member = aaka_config.member_by_sender(requester)
    if not aaka_config.member_is_admin(member.get("id") if member else None):
        return "🔒 Admin only."
    parts = message.strip().split(None, 1)
    if len(parts) < 2:
        return "Usage: /approve <sender_id>"
    sender_id = parts[1].strip()
    _write_sender_list("approved_senders.json", sender_id)
    count = _cancel_sender_approvals(sender_id)
    return (
        f"✅ {sender_id} approved.\n"
        f"Cleared {count} pending request(s).\n"
        "To grant full access, add them to aaka.yaml."
    )


def _handle_deny_sender(message: str, requester: str) -> str:
    """Block an unknown sender — silences future approval messages, clears queue items."""
    member = aaka_config.member_by_sender(requester)
    if not aaka_config.member_is_admin(member.get("id") if member else None):
        return "🔒 Admin only."
    parts = message.strip().split(None, 1)
    if len(parts) < 2:
        return "Usage: /deny <sender_id>"
    sender_id = parts[1].strip()
    _write_sender_list("blocked_senders.json", sender_id)
    count = _cancel_sender_approvals(sender_id)
    return f"🚫 {sender_id} blocked. Cleared {count} pending request(s)."


# ── Outbox flush (Executor → Sensor feedback path) ────────────────────────────

def _flush_outbox() -> None:
    """Send any pending outbox items written by Executor, then mark them sent.

    NOTE: This must NOT be called from within an openclaw-spawned process
    (e.g. route()) because `openclaw message send` deadlocks when the gateway
    is waiting for this script to exit.  Use flush_outbox.py via cron instead.
    """
    from message_send import send
    for item in read_pending_outbox():
        try:
            send(
                item["text"],
                channel_id=item["channel_id"],
                sender=item["sender"],
                reply_to_message_id=item.get("reply_to_message_id"),
            )
            mark_outbox_sent(item["id"], "sent")
        except Exception:
            mark_outbox_sent(item["id"], "error")


# ── Read reaction + threaded reply helpers ────────────────────────────────────

def _react_read(message_id: "str | None", source: str, channel_id: str = "") -> None:
    """Send 👀 reaction to acknowledge receipt (best-effort, Telegram only)."""
    if not message_id or source != "telegram":
        return
    chat_id = channel_id or os.environ.get("TELEGRAM_USER_ID", "")
    if not chat_id:
        return
    try:
        from gateway.egress import send as _egress_send, reaction as _egress_reaction
        _egress_send(_egress_reaction(chat_id, "telegram", "👀", message_id,
                                     source="router_sensor"))
    except Exception:
        pass  # non-fatal


def _reply(
    text: str,
    *,
    channel_id: str,
    sender: str,
    message_id: "str | None",
    dry_run: bool,
    source: str = "",
) -> str:
    """Return text for OpenClaw to send back to the originating conversation.

    We cannot call `openclaw message send` from within a process spawned by
    openclaw — it deadlocks because the gateway is blocked waiting for this
    script to exit.  Instead, return the text on stdout and let openclaw
    handle delivery natively.
    """
    return text


# ── Main routing logic ─────────────────────────────────────────────────────────

_HELP_MAP = {
    "/cal":     "skills.calendar.prepare_event",
    "/plan":    "skills.calendar.plan",
    "/block":   "skills.calendar.block",
    "/tqueue":  "skills.test.tqueue",
    "/tthread": "skills.test.tthread",
    "/texec":   "skills.test.texec",
    "/tstatus": "skills.test.tstatus",
    "/drop":    "skills.drop.drop_file",
    "/note":    "skills.notes.note_writer",
    "/notes":   "skills.notes.note_writer",
    "/n":       "skills.notes.note_writer",
}


def _check_help(message: str) -> "str | None":
    m = re.match(r'^(?:help\s+(/?\w+)|(/\w+)\s+help)\s*$', message.strip(), re.IGNORECASE)
    if not m:
        return None
    cmd = (m.group(1) or m.group(2)).lower()
    if not cmd.startswith("/"):
        cmd = "/" + cmd
    module_path = _HELP_MAP.get(cmd)
    if not module_path:
        return None
    import importlib
    return f"{REPLY_PREFIX}{importlib.import_module(module_path).describe()}"


def _route_impl(raw_input: str, dry_run: bool = False) -> str:
    """Route an incoming message and return the reply string.

    In dry_run mode, no queue writes happen and no block/audit checks run.
    """
    # Debug: log raw input to file (stderr is swallowed by openclaw CLI backend)
    import logging
    _log = logging.getLogger("sensor.route")
    try:
        _debug_path = Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "logs" / "route_debug.log"
        with open(_debug_path, "a") as _df:
            import datetime as _dt
            _df.write(f"\n=== {_dt.datetime.utcnow().isoformat()} raw_input ({len(raw_input)} chars) ===\n")
            _df.write(raw_input)
            _df.write(f"\n=== env: OPENCLAW_MEDIA_PATH={os.environ.get('OPENCLAW_MEDIA_PATH', '(unset)')} ===\n")
            _df.write(f"=== env: OPENCLAW_MEDIA_TYPE={os.environ.get('OPENCLAW_MEDIA_TYPE', '(unset)')} ===\n")
    except Exception:
        pass

    # ── Parse inbound message (block check + audit + format normalisation) ────
    from gateway.ingress import (
        InboundMessage as _IM, receive as _ingress_receive, normalize as _normalize,
    )
    _inbound = _IM(
        raw_text=raw_input,
        sender_id=os.environ.get("OPENCLAW_SENDER", ""),
        channel="whatsapp",   # default; normalize() overrides from envelope
        source="router_sensor",
    )
    # dry_run: parse only (no block check or audit log)
    # production: parse + block check + audit log
    _parsed = _normalize(_inbound) if dry_run else _ingress_receive(_inbound)
    if _parsed is None:
        return ""  # blocked (non-dry_run) or unparseable; ingress already logged it

    message = _parsed.text
    # Normalize Telegram /cmd@botname format → /cmd
    message = re.sub(r'^(/\w+)@\w+', r'\1', message)
    # Strip leading @botname mention (group messages may start with it)
    message = re.sub(r'^@\w+\s*', '', message).strip()
    media = (
        {
            "media_path":        _parsed.media_path,
            "mime_type":         _parsed.mime_type,
            "original_filename": _parsed.original_filename or os.path.basename(_parsed.media_path or ""),
        }
        if _parsed.media_path else None
    )
    # Debug: append parsed results
    try:
        with open(_debug_path, "a") as _df:
            _df.write(f"=== parsed: sender={_parsed.sender_id} channel={_parsed.channel} "
                      f"channel_id={_parsed.channel_id} media={_parsed.media_path} message={message!r} ===\n")
    except Exception:
        pass
    if not message:
        return f"{REPLY_PREFIX}(empty message)"

    # ── Group default (notes-style) — applied BEFORE shortcut expansion ───────
    # If the channel belongs to a `purpose: notes` group and the user didn't
    # slash-escape, force `/note <default_tag> <text>` so the rest of the
    # router treats it as a note no matter what keywords are in the text.
    # File/notifications groups are handled in the late path so they can
    # still benefit from intent matching.
    if not message.lstrip().startswith("/"):
        _early_cid = _parsed.channel_id
        _early_grp = aaka_config.group_for(_early_cid) if _early_cid else None
        if _early_grp and _early_grp.get("purpose") == "notes" and message.strip():
            _tag = _early_grp.get("default_tag", "inbox")
            # Respect explicit `n <tag> ...` or `/note <tag> ...` (rare since
            # bare `n` is the shortcut, but be defensive).
            if not re.match(r"^\s*(?:n|/note)\s+\w+\s+", message, flags=re.I):
                message = f"/note {_tag} {message.strip()}"

    # Single-letter aliases: "b rossmann soap" → "/buy rossmann soap"
    _LETTER_ALIASES = {
        "s": "status", "c": "cal", "q": "tstatus",
        "f": "drop", "b": "buy", "d": "today", "w": "week", "x": "expense", "p": "pdf",
        "m": "mail",
    }
    # "n" is special: "n topic body" → /note (write), "n topic" or bare "n" → /notes (read)
    # "n topic <N>" where N is a positive integer → /notes (read N lines), not a write.
    # Handles: lowercase n, uppercase N, and /n slash-prefix.
    # A leading \n between the alias and topic is silently ignored.
    def _note_route(rest: str) -> str:
        _fl_words = rest.split('\n')[0].split() if rest else []
        _has_multiline_body = '\n' in rest and rest.split('\n', 1)[1].strip()
        _is_read_with_limit = (
            len(_fl_words) == 2 and _fl_words[1].isdigit() and not _has_multiline_body
        )
        _has_body = (len(_fl_words) >= 2 or _has_multiline_body) and not _is_read_with_limit
        return ("/note " if _has_body else "/notes ") + rest
    if re.match(r'^[nN](\s|$)', message) and not message.startswith("/"):
        message = _note_route(message[1:].strip())
    elif re.match(r'^/n(\s|$)', message):
        message = _note_route(message[2:].strip())
    # "t" is special: bare "t" → /tasks; "t <view-kw>" → /tasks <kw>; "t body" → /addtask body
    _TASKS_VIEW_KW = {"help", "all", "overdue", "late", "today", "week", "nodate", "inbox", "undated"}
    if re.match(r'^[tT](\s|$)', message) and not message.startswith("/"):
        rest = message[1:].strip()
        first_kw = rest.lower().split()[0] if rest else ""
        if not rest:
            message = "/tasks"
        elif first_kw in _TASKS_VIEW_KW:
            message = "/tasks " + rest
        else:
            message = "/addtask " + rest
    elif re.match(r'^/t(\s|$)', message):
        rest = message[2:].strip()
        first_kw = rest.lower().split()[0] if rest else ""
        if not rest:
            message = "/tasks"
        elif first_kw in _TASKS_VIEW_KW:
            message = "/tasks " + rest
        else:
            message = "/addtask " + rest
    # Normalize slash-less commands: "cal physio tomorrow" → "/cal physio tomorrow"
    _SLASH_COMMANDS = {"cal", "fix", "plan", "day", "block", "today", "week",
                       "addtask", "task", "tasks", "done", "complete", "edit", "del", "delete",
                       "menu", "status", "outbox", "llm",
                       "note", "notes", "drop", "buy", "engage", "expense", "budget",
                       "mail", "pw", "pass"}
    first_word = message.split()[0].lower() if message else ""
    if len(first_word) == 1 and first_word.lower() in _LETTER_ALIASES and not message.startswith("/"):
        message = "/" + _LETTER_ALIASES[first_word.lower()] + message[1:]
        first_word = message.split()[0].lower()  # re-derive after expansion
    if first_word in _SLASH_COMMANDS and not message.startswith("/"):
        message = "/" + message

    help_reply = _check_help(message)
    if help_reply:
        return help_reply

    sender_id  = _parsed.sender_id
    channel_id = _parsed.channel_id
    source     = _parsed.channel
    sender_name = getattr(_parsed, "sender_name", "") or ""
    sender_email = ""
    message_id = _parsed.message_id

    # Self-DM: sender is the connected WhatsApp phone, not the actual user.
    # Map to the DM binding owner (WHATSAPP_SELF_MEMBER env var, default: first parent).
    if _parsed.is_self_dm and not aaka_config.member_by_sender(sender_id):
        self_member_id = os.environ.get("WHATSAPP_SELF_MEMBER", "")
        if self_member_id:
            m = aaka_config.member_by_name(self_member_id)
        else:
            # Default: first adult member
            m = next((m for m in aaka_config.members() if m.get("role") == "adult"), None)
        if m:
            # Rewrite sender_id to the member's configured whatsapp so downstream lookups work
            sender_id = m.get("whatsapp") or m.get("telegram") or sender_id
            _log.info("self-dm: mapped sender to member=%s sender_id=%s", m["id"], sender_id)

    msg_lower = message.lower()

    # Log enough to discover WhatsApp JIDs from container logs on first run
    _log.info("recv source=%s sender=%s channel=%s msg=%.80r", source, sender_id, channel_id, message)

    # ── Channel gate — drop unknown senders/groups ────────────────────────────
    if not dry_run and not _is_allowed_channel(sender_id, channel_id):
        _log.info("drop source=%s sender=%s channel=%s (not in allowlist)", source, sender_id, channel_id)
        # Invite-code onboarding: a valid one-time code in the message
        # auto-registers the sender (binds their handle → member) and welcomes
        # them, before the "ask the admin" fallback. DMs only.
        if sender_id == channel_id:
            try:
                from sensor import wa_onboard
                welcome = wa_onboard.try_signup(sender_id, sender_name, message)
                if welcome:
                    _log.info("signup source=%s sender=%s — bound via invite code", source, sender_id)
                    return welcome
            except Exception as _e:  # pragma: no cover - defensive
                _log.warning("invite signup check failed: %s", _e)
        # For DMs, reply with the sender's own handle so they can be added.
        # Group messages silently drop (no way to know intent).
        if source == "telegram" and sender_id == channel_id:
            return (
                f"👋 Hi! You're almost in — I just need to know this is you.\n\n"
                f"Your Telegram ID is `{sender_id}`.\n\n"
                f"Share this with whoever set me up and they'll add you in a few seconds."
            )
        if source == "whatsapp" and sender_id == channel_id:
            # Show the handle to add to the allowlist. Real numbers → +E.164; a
            # privacy @lid has no phone equivalent, so show it verbatim (no bogus +).
            if sender_id.endswith("@s.whatsapp.net"):
                num = sender_id.replace("@s.whatsapp.net", "")
                if num and not num.startswith("+"):
                    num = "+" + num
                what = f"Your WhatsApp number is `{num}`"
            else:
                what = f"Your WhatsApp handle is `{sender_id}`"
            greeting = f"👋 Hi{(' ' + sender_name) if sender_name else ''}! You're almost in"
            return (
                f"{greeting} — I don't recognise you yet.\n\n"
                f"{what}.\n\n"
                f"Share it with whoever set me up and they'll add you in a few seconds."
            )
        return ""

    # ── Acknowledge receipt with 👀 reaction ──────────────────────────────────
    if not dry_run:
        _react_read(message_id, source, channel_id)

    # ── Outbox flush skipped here — openclaw subprocess deadlocks when called
    #    from within an openclaw-spawned process.  Outbox items are flushed by
    #    sensor/flush_outbox.py via cron instead.

    # ── Pending-confirm state (queue-based) ───────────────────────────────────
    if not dry_run:
        pc = get_pending_confirm(sender_id)
        if pc:
            item_id = pc["item_id"]

            # ── List shortcuts: bare numbers, #done, #clear after showing a list ───
            if item_id.startswith("list:"):
                list_name = item_id[5:]
                _list_ns = _namespace_for_sender(sender_id) or "shared"
                msg_stripped = message.strip().lower()
                # Bare numbers (e.g. "1 3") → check off those items
                if re.match(r'^[\d\s]+$', msg_stripped):
                    from skills.lists.list_manager import check_off, show as _show_list
                    result = check_off(list_name, message.strip(), _list_ns)
                    _queue_file_sync(list_name, sender_id, channel_id, source=source)
                    remaining = _show_list(list_name, _list_ns)
                    set_pending_confirm(sender_id, item_id)
                    return _reply(f"{REPLY_PREFIX}{result}\n\n{remaining}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                # "#done" → confirm then mark all done
                if msg_stripped in ("#done", "done"):
                    from skills.lists.list_manager import preview_done_all
                    preview = preview_done_all(list_name, _list_ns)
                    set_pending_confirm(sender_id, f"list-done:{list_name}")
                    return _reply(f"{REPLY_PREFIX}{preview}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                # "#clear" → confirm then archive done items
                if msg_stripped in ("#clear", "clear"):
                    from skills.lists.list_manager import preview_clear
                    preview = preview_clear(list_name, _list_ns)
                    set_pending_confirm(sender_id, f"list-clear:{list_name}")
                    return _reply(f"{REPLY_PREFIX}{preview}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                # Any other message clears the list state and routes normally
                clear_pending_confirm(sender_id)

            # ── List #done / #clear confirmation: "y" confirms, anything else cancels ─
            if item_id.startswith("list-done:"):
                list_name = item_id[10:]
                _list_ns = _namespace_for_sender(sender_id) or "shared"
                clear_pending_confirm(sender_id)
                if message.strip().lower() in ("y", "yes"):
                    from skills.lists.list_manager import done_all
                    result = done_all(list_name, _list_ns)
                    _queue_file_sync(list_name, sender_id, channel_id, source=source)
                    return _reply(f"{REPLY_PREFIX}{result}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                return _reply(f"{REPLY_PREFIX}Cancelled.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

            # ── List ideas: "add 1 3" or bare "1 3" picks from the ideas pool ─
            if item_id.startswith("list-ideas:"):
                list_name = item_id[11:]
                _list_ns = _namespace_for_sender(sender_id) or "shared"
                msg_stripped = message.strip()
                m_add = re.match(r'^\s*(?:add\s+)?([\d\s]+)$', msg_stripped, re.I)
                if m_add:
                    from skills.lists.list_manager import ideas, add_items, list_state
                    from skills.lists.format import render_list_view
                    data = ideas(list_name, _list_ns)
                    nums = re.findall(r'\b(\d+)\b', m_add.group(1))
                    picked = []
                    for n in nums:
                        idx = int(n) - 1
                        if 0 <= idx < len(data["ideas"]):
                            picked.append(data["ideas"][idx]["item"])
                    if not picked:
                        clear_pending_confirm(sender_id)
                        return _reply(f"{REPLY_PREFIX}No ideas at those numbers in *{list_name}*.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                    add_items(list_name, picked, _list_ns)
                    _queue_file_sync(list_name, sender_id, channel_id, source=source)
                    state = list_state(list_name, _list_ns)
                    set_pending_confirm(sender_id, f"list:{list_name}")
                    rendered = render_list_view(
                        state["list_name"], state["unchecked"], state["checked_count"], state["shared"],
                        added_count=len(picked),
                    )
                    return _reply(f"{REPLY_PREFIX}{rendered}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                # Anything else clears the ideas state and routes normally.
                clear_pending_confirm(sender_id)

            if item_id.startswith("list-clear:"):
                list_name = item_id[11:]
                _list_ns = _namespace_for_sender(sender_id) or "shared"
                clear_pending_confirm(sender_id)
                if message.strip().lower() in ("y", "yes"):
                    from skills.lists.list_manager import clear_done
                    result = clear_done(list_name, _list_ns)
                    _queue_file_sync(list_name, sender_id, channel_id, source=source)
                    return _reply(f"{REPLY_PREFIX}{result}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                return _reply(f"{REPLY_PREFIX}Cancelled.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

            # ── Drop-ask confirmation: user replies with tags or "skip" ────────
            if item_id.startswith("drop-ask:"):
                staging_id = item_id[9:]
                clear_pending_confirm(sender_id)

                config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
                staging_dir = config_dir / "data" / "staging" / staging_id

                # Parse inline button callbacks
                msg_text = message.strip()
                if msg_text.startswith("drop-skip:"):
                    msg_text = "skip"
                tag_cb = re.match(r'^drop-tag:[a-f0-9]+:(.+)$', msg_text)
                if tag_cb:
                    msg_text = tag_cb.group(1)

                if msg_text.lower() in ("skip", "no", "cancel"):
                    try:
                        import shutil
                        if staging_dir.exists():
                            shutil.rmtree(str(staging_dir))
                    except Exception:
                        pass
                    return _reply(f"{REPLY_PREFIX}⏭ Skipped — file discarded.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

                # Parse words as tags
                words = msg_text.split()
                tags = []
                for w in words:
                    w_clean = w.lstrip('#').lower()
                    if w_clean and len(w_clean) <= 20 and re.match(r'^[a-z0-9_-]+$', w_clean):
                        tags.append(w_clean)

                # Find staged file
                staged_files = list(staging_dir.iterdir()) if staging_dir.exists() else []
                if not staged_files:
                    return _reply(f"{REPLY_PREFIX}⚠️ Staged file not found (expired?).", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

                staged_file = staged_files[0]
                safe_name = staged_file.name
                namespace = _namespace_for_sender(sender_id) or (aaka_config.carriers()[0] if aaka_config.carriers() else "user")
                file_size = staged_file.stat().st_size

                drop_payload = {
                    "tags": tags,
                    "media_staging_path": f"staging/{staging_id}/{safe_name}",
                    "mime_type": "",
                    "original_filename": safe_name,
                    "file_size_bytes": file_size,
                    "caption": msg_text,
                    "namespace": namespace,
                    "message_id": message_id,
                    "source": source,
                }
                new_item_id = write_item(
                    intent="drop_file", raw_message=msg_text, sender=sender_id,
                    channel_id=channel_id, source=source, payload=drop_payload,
                )
                update_status(new_item_id, "confirmed")

                size_kb = file_size // 1024
                from skills.drop.format import render_staged_ack
                from tools.inbox_router import preview_destination
                dest = preview_destination(tags, namespace) if tags else None
                msg = render_staged_ack(
                    safe_name=safe_name, size_kb=size_kb, tags=tags,
                    destination=dest, hash_line=f"\n`#{new_item_id[:8]}`",
                )
                return _reply(
                    f"{REPLY_PREFIX}{msg}",
                    channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source,
                )

            item = get_item(item_id)
            payload = json.loads(item["payload"]) if item else {}

            # "llm" re-process: re-extract via LLM and show fresh confirmation
            if re.search(r'\bllm\b', msg_lower) and item and item.get("intent") == "add_event":
                try:
                    from skills.calendar.prepare_event import extract_events
                    from skills.calendar.add_event import build_title
                    sender_member = aaka_config.member_by_sender(sender_id)
                    sender_mid_id = sender_member["id"] if sender_member else ""
                    new_payload = extract_events(
                        item["raw_message"], sender_email=sender_email,
                        force_llm=True, sender_member_id=sender_mid_id
                    )
                    try:
                        from skills.calendar.availability import conflicts_in_window
                        conflicts, checked_labels = conflicts_in_window(
                            new_payload.get("occurrences", []),
                            sender_mid_id,
                            extra_member_ids=new_payload.get("conflict_members", []),
                            blocker_members=[new_payload.get("attendee", ""), new_payload.get("carrier", "")],
                        )
                        new_payload["conflicts"] = conflicts
                        new_payload["checked_calendar_labels"] = checked_labels
                    except Exception:
                        pass
                    new_payload["message_id"] = payload.get("message_id") or message_id
                    update_payload(item_id, new_payload)
                    preview = _format_event_preview(new_payload)
                    orig_msg_id = new_payload.get("message_id") or message_id
                    return _reply(f"{REPLY_PREFIX}{preview}\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)
                except Exception as exc:
                    return _reply(f"{REPLY_PREFIX}⚠️ LLM re-extraction failed: {exc}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

            # "alone" reply: mark no_carrier and proceed
            if re.search(r'\balone\b', msg_lower) and item and item.get("intent") == "add_event":
                from skills.calendar.add_event import build_title
                payload["no_carrier"] = True
                payload["carrier"] = ""
                payload["carrier_required"] = False
                payload["title"] = build_title(
                    payload.get("attendee") or aaka_config.group_name(),
                    payload.get("type", "other"),
                    payload.get("summary", "Event"),
                    no_carrier=True,
                )
                update_payload(item_id, payload)
                clear_pending_confirm(sender_id)
                update_status(item_id, "confirmed")
                orig_msg_id = payload.get("message_id") or message_id
                return _reply(f"{REPLY_PREFIX}✅ Queued for execution (going alone).\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)

            # ── Batch event confirm (add_event_batch) ─────────────────────────
            if item and item.get("intent") == "add_event_batch":
                orig_msg_id = payload.get("message_id") or message_id
                all_events = payload.get("events", [])
                n_total = len(all_events)

                # Parse "yes 1 3" / "cancel 2" / "yes" / "cancel"
                nums_in_msg = [int(x) for x in re.findall(r'\b([1-9]\d?)\b', message) if 1 <= int(x) <= n_total]
                is_batch_confirm = _is_confirm(msg_lower)
                is_batch_cancel  = _is_cancel(msg_lower)

                if is_batch_cancel and not is_batch_confirm:
                    # "cancel" alone or "cancel 2" — cancel specific items or all
                    if nums_in_msg:
                        cancel_idx = {i - 1 for i in nums_in_msg}
                        kept = [ev for i, ev in enumerate(all_events) if i not in cancel_idx]
                        if kept:
                            payload["events"] = kept
                            update_payload(item_id, payload)
                            from skills.calendar.prepare_event import format_batch_review
                            preview = format_batch_review(kept)
                            return _reply(f"{REPLY_PREFIX}{preview}\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)
                    # cancel all
                    clear_pending_confirm(sender_id)
                    update_status(item_id, "cancelled")
                    return _reply(f"{REPLY_PREFIX}❌ Cancelled.\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)

                if is_batch_confirm:
                    # "yes 2 3" — confirm only listed items; "yes" = confirm all
                    confirmed_events = [all_events[i - 1] for i in nums_in_msg] if nums_in_msg else all_events
                    payload["events"] = confirmed_events
                    update_payload(item_id, payload)
                    clear_pending_confirm(sender_id)
                    update_status(item_id, "confirmed")
                    n = len(confirmed_events)
                    word = "event" if n == 1 else f"{n} events"
                    return _reply(f"{REPLY_PREFIX}✅ Queued {word} for execution.\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)

                # Unrecognised reply while batch is pending — re-show preview
                from skills.calendar.prepare_event import format_batch_review
                preview = format_batch_review(all_events)
                return _reply(f"{REPLY_PREFIX}{preview}\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)

            # ── Skip individual occurrences for multi-occurrence single event ──
            if item and item.get("intent") == "add_event":
                skip_match = re.match(r'^(?:yes\s+)?skip\s+([\d\s]+)', msg_lower)
                if skip_match:
                    occs = payload.get("occurrences", [])
                    n_occs = len(occs)
                    if n_occs > 1:
                        skip_nums = {int(x) for x in re.findall(r'\d+', skip_match.group(1))
                                     if 1 <= int(x) <= n_occs}
                        if skip_nums:
                            kept = [o for i, o in enumerate(occs, 1) if i not in skip_nums]
                            orig_msg_id = payload.get("message_id") or message_id
                            if not kept:
                                clear_pending_confirm(sender_id)
                                update_status(item_id, "cancelled")
                                return _reply(
                                    f"{REPLY_PREFIX}❌ All dates skipped — cancelled.\n`#{item_id[:8]}`",
                                    channel_id=channel_id, sender=sender_id,
                                    message_id=orig_msg_id, dry_run=dry_run, source=source,
                                )
                            payload["occurrences"] = kept
                            payload.pop("conflict_warned", None)
                            try:
                                from skills.calendar.availability import conflicts_in_window
                                sender_mid = aaka_config.member_by_sender(sender_id)
                                sender_mid_id = sender_mid["id"] if sender_mid else ""
                                conflicts, checked_labels = conflicts_in_window(
                                    kept, sender_mid_id,
                                    extra_member_ids=payload.get("conflict_members", []),
                                    blocker_members=[payload.get("attendee", ""),
                                                     payload.get("carrier", "")],
                                )
                                payload["conflicts"] = conflicts
                                payload["checked_calendar_labels"] = checked_labels
                            except Exception:
                                pass
                            update_payload(item_id, payload)
                            skipped_str = ", ".join(str(s) for s in sorted(skip_nums))
                            preview = _format_event_preview(payload)
                            return _reply(
                                f"{REPLY_PREFIX}Skipped date(s) {skipped_str}.\n\n{preview}\n`#{item_id[:8]}`",
                                channel_id=channel_id, sender=sender_id,
                                message_id=orig_msg_id, dry_run=dry_run, source=source,
                            )

            mods, feedback = _parse_confirm_modifiers(msg_lower, payload)
            is_confirm = _is_confirm(msg_lower) or bool(mods)
            if is_confirm:
                if mods:
                    payload = _apply_confirm_modifiers(payload, mods)
                    update_payload(item_id, payload)
                # Conflict check for add_event (skip if already warned or user said 'force')
                item_intent = item["intent"] if item else ""
                is_force = bool(re.match(r'^force\b', msg_lower))
                if item_intent == "add_event" and not payload.get("conflict_warned") and not is_force:
                    try:
                        from skills.calendar.availability import conflicts_in_window
                        attendee   = payload.get("attendee", "")
                        carrier    = payload.get("carrier", "")
                        sender_mid = aaka_config.member_by_sender(sender_id)
                        sender_mid_id = sender_mid["id"] if sender_mid else ""
                        conflicts, checked_labels = conflicts_in_window(
                            payload.get("occurrences", []),
                            sender_mid_id,
                            extra_member_ids=payload.get("conflict_members", []),
                            blocker_members=[attendee, carrier],
                        )
                        if conflicts:
                            payload["conflicts"] = conflicts
                            payload["checked_calendar_labels"] = checked_labels
                            has_blockers = any(c.get("is_blocker") for c in conflicts)
                            if not has_blockers:
                                # Only nearby/sender conflicts — save for display but don't gate on force
                                update_payload(item_id, payload)
                            else:
                                payload["conflict_warned"] = True
                                update_payload(item_id, payload)
                                set_pending_confirm(sender_id, item_id)
                                occs = payload.get("occurrences", [])
                                from skills.calendar.prepare_event import _short_dt, _conflict_label
                                if len(occs) > 1 and any(o.get("occ_conflicts") for o in occs):
                                    lines = ["⚠️ Some dates have conflicts:"]
                                    for i, occ in enumerate(occs, 1):
                                        occ_c = occ.get("occ_conflicts", [])
                                        if occ_c:
                                            dt = _short_dt(occ)
                                            clash = ", ".join(_conflict_label(c) + f" {c['start']}–{c['end']}" for c in occ_c[:2])
                                            cicon = "🚫" if any(c.get("is_blocker") for c in occ_c) else "⚠️ "
                                            lines.append(f"  {i}. {dt}  {cicon} {clash}")
                                    lines.append("\nReply 'force' to save all, 'skip 2 4' to remove clashing dates, or 'cancel'.")
                                elif len(occs) > 1:
                                    # Multi-occ but occ_conflicts not populated — show per-conflict with member
                                    lines = ["⚠️ Conflicts detected:"]
                                    for c in conflicts:
                                        icon = "🚫" if c.get("is_blocker") else "⚠️ "
                                        label = _conflict_label(c)
                                        lines.append(f"  {icon} {label}  {c['start']}–{c['end']}")
                                    if checked_labels:
                                        lines.append("Checked: " + ", ".join(checked_labels))
                                    lines.append("\nReply 'force' to save all, 'skip N' to remove dates, or 'cancel'.")
                                else:
                                    lines = ["⚠️ Conflict detected:"]
                                    for c in conflicts:
                                        icon = "🚫" if c.get("is_blocker") else "⚠️ "
                                        label = _conflict_label(c)
                                        lines.append(f"  {icon} {label}  {c['start']}–{c['end']}")
                                    if checked_labels:
                                        lines.append("Checked: " + ", ".join(checked_labels))
                                    lines.append("\nReply 'force' to save anyway, or 'cancel' to abort.")
                                orig_msg_id = payload.get("message_id") or message_id
                                return _reply(f"{REPLY_PREFIX}" + "\n".join(lines), channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)
                    except Exception:
                        pass  # non-fatal; proceed with confirm
                # ── VPS direct execution for calendar intents ─────────────────
                if _VPS_TOKEN.exists() and item_intent in ("add_event", "add_event_batch", "block_cal"):
                    try:
                        _vps_result = _exec_cal_direct_vps(item_intent, payload)
                        clear_pending_confirm(sender_id)
                        update_status(item_id, "done")
                        suffix = f"\n{feedback}" if feedback else ""
                        orig_msg_id = payload.get("message_id") or message_id
                        return _reply(f"{REPLY_PREFIX}{_vps_result}{suffix}\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)
                    except Exception as _direct_exc:
                        logging.getLogger("sensor").warning(
                            "VPS direct cal write failed (%s) — falling back to queue", _direct_exc
                        )
                        # Fall through to queue path below
                clear_pending_confirm(sender_id)
                # For approval-gated intents with a future schedule_at, go to 'scheduled'
                _sched_at = item.get("schedule_at") if item else None
                if _sched_at:
                    from datetime import datetime as _cdt, timezone as _ctz
                    try:
                        _sched_dt = _cdt.strptime(_sched_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_ctz.utc)
                        if _sched_dt > _cdt.now(_ctz.utc):
                            update_status(item_id, "scheduled")
                            orig_msg_id = payload.get("message_id") or message_id
                            return _reply(
                                f"{REPLY_PREFIX}✅ Approved. Will post at {_sched_at[:16].replace('T', ' ')} UTC.\n`#{item_id[:8]}`",
                                channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source,
                            )
                    except ValueError:
                        pass
                update_status(item_id, "confirmed")
                suffix = f"\n{feedback}" if feedback else ""
                orig_msg_id = payload.get("message_id") or message_id
                return _reply(f"{REPLY_PREFIX}✅ Queued for execution.{suffix}\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)
            if _is_cancel(msg_lower):
                clear_pending_confirm(sender_id)
                update_status(item_id, "cancelled")
                orig_msg_id = payload.get("message_id") or message_id
                return _reply(f"{REPLY_PREFIX}❌ Cancelled.\n`#{item_id[:8]}`", channel_id=channel_id, sender=sender_id, message_id=orig_msg_id, dry_run=dry_run, source=source)

    # ── Agent reply intercept ────────────────────────────────────────────────
    # Runs only when aaka has no pending_confirm for this sender.
    # If an external agent is waiting for a reply, capture it and short-circuit —
    # UNLESS the message is an unambiguous slash command (e.g. /tasks, /menu),
    # in which case the user is clearly addressing aaka, not the agent.
    if not dry_run and not (message or "").lstrip().startswith("/"):
        _arr = get_active_reply_request(sender_id)
        if _arr:
            record_agent_reply(_arr["correlation_id"], _arr["agent_id"], sender_id, message)
            _log.info(
                "agent_reply: corr=%s agent=%s sender=%s",
                _arr["correlation_id"], _arr["agent_id"], sender_id,
            )
            return _reply(
                f"{REPLY_PREFIX}✅ Reply received.",
                channel_id=channel_id,
                sender=sender_id,
                message_id=message_id,
                dry_run=dry_run,
                source=source,
            )

    # ── Intent matching ───────────────────────────────────────────────────────
    intent = match_intent(message)

    if _ENABLED_INTENTS and intent and intent not in _ENABLED_INTENTS:
        intent = None

    # ── Media auto-detect: photo/file without /drop → smart prompt or drop ──
    if intent is None and media is not None:
        if message == "__drop_ask__":
            intent = "drop_ask"
        else:
            intent = "drop_file"  # backward compat: unknown text + media

    # ── Files-group nudge — text-only message with no matching intent ───────
    # (Notes-group rewrite happens earlier, before shortcut expansion.)
    if intent is None and media is None and message.strip() and not message.lstrip().startswith("/"):
        _grp = aaka_config.group_for(channel_id)
        if _grp and _grp.get("purpose") == "files":
            return _reply(
                f"{REPLY_PREFIX}This is the *files* group — attach a file or use /menu commands.",
                channel_id=channel_id, sender=sender_id, message_id=message_id,
                dry_run=dry_run, source=source,
            )

    # ── aaka Tools: a message matching a registered tool's `command` runs it ──
    # (checked after core intents so tools can't shadow built-ins — aaka owns the
    # namespace). Only for recognized senders; admin gate lives per-tool if needed.
    if intent is None and message.lstrip().startswith("/"):
        try:
            from sensor import tool_runner
            cmd = message.strip().split()[0].lower()
            extra = message.strip()[len(cmd):].strip()
            for tname, tentry in tool_runner.load_manifest().items():
                tcmd = (tentry.get("command") or "").lower()
                if tcmd and tcmd == cmd and tentry.get("enabled", True):
                    res = tool_runner.run_and_report(tname, extra)
                    head = "✅" if res.get("ok") else "⚠️"
                    return _reply(f"{head} {res.get('summary') or res.get('error') or tname}",
                                  channel_id=channel_id, sender=sender_id,
                                  message_id=message_id, dry_run=dry_run, source=source)
        except Exception:
            pass

    if intent is None:
        return _reply(f"{REPLY_PREFIX}Sorry, I didn't understand that. ↪ /menu", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # Track engagement (silent — never raises)
    if not dry_run and sender_id:
        _sender_member = aaka_config.member_by_sender(sender_id)
        if _sender_member:
            try:
                from skills.engagement.tracker import track_event
                track_event(_sender_member["id"], intent, channel=source or "")
            except Exception:
                pass

    if intent == "pay":
        return _handle_pay(message, channel_id=channel_id, sender=sender_id,
                           message_id=message_id, dry_run=dry_run, source=source)

    if intent == "invite":
        return _reply(_handle_invite(message, sender_id),
                      channel_id=channel_id, sender=sender_id,
                      message_id=message_id, dry_run=dry_run, source=source)

    if intent == "tools_list":
        return _reply(_handle_tools(message, sender_id),
                      channel_id=channel_id, sender=sender_id,
                      message_id=message_id, dry_run=dry_run, source=source)

    if intent in _LOCAL_INTENTS:
        text = _handle_local_intent(intent, message=message, sender=sender_id, channel_id=channel_id, source=source)
        return _reply(text, channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── bday_wish: send birthday message to a contact via WhatsApp or email ───
    if intent == "bday_wish":
        test_mode = bool(re.search(r'\btest\b', message, re.I))
        num_m = re.search(r'\d+', message)
        if not num_m:
            text = "Usage: bday wish N  (add 'test' to preview without sending)"
        else:
            from skills.contacts.birthday_list import _load_item as _bday_load_item
            num = int(num_m.group())
            item = _bday_load_item(sender_id, num)
            if not item:
                text = "No birthday list — send `bday` first (lists expire after 24h)."
            else:
                phone = item.get("mobile", "")
                email = item.get("email", "")
                wish_msg = f"Happy Birthday {item['first_name']}! 🎂"

                if not phone and not email:
                    text = f"No contact info for {item['name']} — add phone or email to contacts."
                elif test_mode:
                    ch = "whatsapp" if phone else "email"
                    addr = phone if phone else email
                    text = f"🧪 Would send via {ch}\nTo: {addr}\nMsg: {wish_msg}"
                elif phone:
                    from skills.outbox.send_contact import send_to_contact as _send_contact
                    result = _send_contact(name=item["name"], first_name=item["first_name"],
                                          phone=phone, message=wish_msg, channel="whatsapp",
                                          dry_run=dry_run)
                    if result["sent"]:
                        text = f"✅ Sent wishes to {item['first_name']} via WhatsApp!"
                    else:
                        text = f"⚠️ Could not send: {result.get('error')}"
                else:
                    # email-only: queue to executor (needs Gmail token)
                    _sender_m = aaka_config.member_by_sender(sender_id) or {}
                    if not dry_run:
                        _wi = write_item(
                            intent="bday_wish", raw_message=message,
                            sender=sender_id, channel_id=channel_id, source=source,
                            payload={"name": item["name"], "first_name": item["first_name"],
                                     "email": email, "message": wish_msg,
                                     "from_member_id": _sender_m.get("id", ""),
                                     "channel_id": channel_id, "sender": sender_id,
                                     "source": source},
                        )
                        update_status(_wi, "confirmed")
                    text = f"📧 Queuing birthday email to {item['first_name']}…"
        return _reply(text, channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── flush_outbox: manual /outbox trigger to flush pending outbox items ────
    if intent == "flush_outbox":
        items = read_pending_outbox()
        n = len(items)
        if not dry_run:
            _flush_outbox()
        else:
            print(f"[dry-run] intent=flush_outbox  would flush {n} pending outbox item(s)")
        return _reply(f"{REPLY_PREFIX}📬 Outbox flush: {n} item(s) processed.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── queue_test: no LLM, no confirmation, write directly as 'confirmed' ────
    if intent == "queue_test":
        if not dry_run:
            item_id = write_item(
                intent="queue_test",
                raw_message=message,
                sender=sender_id,
                channel_id=channel_id,
                source=source,
                payload={"message_id": message_id},
            )
            update_status(item_id, "confirmed")
        else:
            print("[dry-run] intent=queue_test  payload={}")
        return _reply(f"{REPLY_PREFIX}Queued command; will run when Executor comes online.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── executor_echo: end-to-end echo test ──────────────────────────────────
    if intent == "executor_echo":
        from datetime import datetime, timezone
        echo_text = re.sub(r'^/texec\b', '', message, flags=re.IGNORECASE).strip()
        enqueued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "echo_text": echo_text,
            "message_id": message_id,
            "enqueued_at": enqueued_at,
            "sender": sender_id,
            "channel_id": channel_id,
            "source": source,
        }
        if not dry_run:
            item_id = write_item(
                intent="executor_echo",
                raw_message=message,
                sender=sender_id,
                channel_id=channel_id,
                source=source,
                payload=payload,
            )
            update_status(item_id, "confirmed")
        else:
            print(f"[dry-run] intent=executor_echo  payload={payload}")
        return _reply(f"{REPLY_PREFIX}✅ Echo queued — executor will reply.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── test_thread: threading smoke test (resolved locally) ─────────────────
    if intent == "test_thread":
        if dry_run:
            print(f"[dry-run] intent=test_thread  would reply to message_id={message_id}")
        return _reply(f"{REPLY_PREFIX}🧵 Thread test — message_id={message_id}.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── test_status: live queue + outbox snapshot ─────────────────────────────
    if intent == "test_status":
        queue_items = read_pending()
        outbox_items = read_pending_outbox()
        lines = [f"{REPLY_PREFIX}🔬 Queue status:"]
        lines.append(f"• Awaiting executor: {len(queue_items)} item(s)")
        for it in queue_items[:3]:
            lines.append(f"  – {it['intent']} [{it['id'][:8]}] status={it['status']}")
        lines.append(f"• Outbox (pending send): {len(outbox_items)} item(s)")
        for it in outbox_items[:3]:
            lines.append(f"  – to={str(it['channel_id'])[:12]} [{it['id'][:8]}]")
        return _reply("\n".join(lines), channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── route_tags: queue to executor (vault/references.yaml lives on Mac) ────
    if intent == "route_tags":
        raw_tags = message.removeprefix("/route").strip().lower().split()
        if not raw_tags:
            return _reply(f"{REPLY_PREFIX}Usage: /route <tags>  e.g. /route flo172 expense", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        payload = {"tags": raw_tags, "message_id": message_id}
        if not dry_run:
            item_id = write_item(
                intent="route_tags", raw_message=message, sender=sender_id,
                channel_id=channel_id, source=source, payload=payload,
            )
            update_status(item_id, "confirmed")
        else:
            print(f"[dry-run] intent=route_tags  payload={payload}")
        return _reply(f"{REPLY_PREFIX}Routing tags — will confirm when executor comes online.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── budget: sensor-side expense tracking ────────────────────────────────────
    #    x 45 groceries lidl → log expense
    #    x → current month summary    x april → that month    x 2026 → yearly
    #    x undo → remove last    x fix 3 55 → change #3 to 55
    if intent == "budget":
        from datetime import date
        from skills.budget.budget_tracker import (
            log_expense, undo as budget_undo, fix as budget_fix,
            summary as budget_summary, yearly as budget_yearly,
            sync_dest as budget_sync_dest, _resolve_month,
        )
        text = re.sub(r'^/(expense|budget)\s*', '', message, flags=re.I).strip()
        namespace = _namespace_for_sender(sender_id) or (aaka_config.carriers()[0] if aaka_config.carriers() else "user")

        if not text:
            return _reply(f"{REPLY_PREFIX}{budget_summary()}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        low = text.lower()

        # undo
        if low == "undo":
            if not dry_run:
                result = budget_undo()
                source_path = f"data/budget/{date.today().strftime('%Y-%m')}.md"
                dest_path = budget_sync_dest(namespace=namespace)
                _queue_note_sync(source_path, dest_path, sender_id, channel_id)
            else:
                result = "[dry-run] budget undo"
                print(f"[dry-run] intent=budget  action=undo")
            return _reply(f"{REPLY_PREFIX}{result}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # fix N amount
        fix_m = re.match(r'^fix\s+(\d+)\s+([\d.]+)$', low)
        if fix_m:
            idx, amt = int(fix_m.group(1)), float(fix_m.group(2))
            if not dry_run:
                result = budget_fix(idx, amt)
                source_path = f"data/budget/{date.today().strftime('%Y-%m')}.md"
                dest_path = budget_sync_dest(namespace=namespace)
                _queue_note_sync(source_path, dest_path, sender_id, channel_id)
            else:
                result = f"[dry-run] budget fix #{idx} → {amt}"
                print(f"[dry-run] intent=budget  action=fix idx={idx} amt={amt}")
            return _reply(f"{REPLY_PREFIX}{result}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # yearly: 4-digit year
        year_m = re.match(r'^(\d{4})$', low)
        if year_m:
            return _reply(f"{REPLY_PREFIX}{budget_yearly(int(year_m.group(1)))}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # month name: "april", "jan", etc.
        resolved = _resolve_month(low)
        if resolved:
            return _reply(f"{REPLY_PREFIX}{budget_summary(resolved)}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # log expense: first token must be a number
        parts = text.split()
        try:
            amount = float(parts[0])
        except (ValueError, IndexError):
            return _reply(
                f"{REPLY_PREFIX}Usage:\n"
                "  x 45 groceries lidl — log expense\n"
                "  x — month summary\n"
                "  x april — that month\n"
                "  x 2026 — yearly\n"
                "  x undo — remove last\n"
                "  x fix 3 55 — change #3 amount",
                channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source,
            )
        category = parts[1].lower() if len(parts) > 1 else "other"
        merchant = " ".join(parts[2:]).lower() if len(parts) > 2 else ""

        if not dry_run:
            source_path = log_expense(amount, category, merchant, namespace)
            dest_path = budget_sync_dest(namespace=namespace)
            _queue_note_sync(source_path, dest_path, sender_id, channel_id)
        else:
            print(f"[dry-run] intent=budget  action=log amount={amount} cat={category} merch={merchant}")
        return _reply(
            f"{REPLY_PREFIX}Logged EUR {amount:.2f} {category}"
            + (f" ({merchant})" if merchant else "")
            + f"\n\n\u21aa x to view \u00b7 x undo to remove",
            channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source,
        )

    # ── keep_original: restore the uncompressed original by hash ────────────────
    if intent == "keep_original":
        hash_match = re.search(r'#?([0-9a-f]{6,})', message, re.I)
        if not hash_match:
            return _reply(f"{REPLY_PREFIX}Usage: keep #abc12345 (the hash shown after a compressed drop)", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        keep_hash = hash_match.group(1).lower()[:8]
        if dry_run:
            print(f"[dry-run] intent=keep_original  keep_hash={keep_hash}")
            return ""
        namespace = _namespace_for_sender(sender_id) or "user"
        from aaka_queue.queue import write_item as _q_write, update_status as _q_up
        keep_payload = {
            "keep_hash": keep_hash,
            "namespace": namespace,
            "message_id": message_id,
            "source": source,
        }
        keep_id = _q_write(
            intent="keep_original", raw_message=message, sender=sender_id,
            channel_id=channel_id, source=source, payload=keep_payload,
        )
        _q_up(keep_id, "confirmed")
        return _reply(f"{REPLY_PREFIX}↩️ Restoring original for #{keep_hash}…", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── undo_queue: cancel or undo a queued action by hash ──────────────────────
    if intent == "undo_queue":
        hash_match = re.search(r'#?([0-9a-f]{6,})', message, re.I)
        if not hash_match:
            return _reply(f"{REPLY_PREFIX}Usage: undo #abc12345", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        prefix = hash_match.group(1).lower()
        try:
            from aaka_queue.queue import update_status as _q_update
            import sqlite3 as _sq
            _db_path = os.environ.get("QUEUE_DB") or str(Path(os.environ.get("AAKA_CONFIG_DIR", "/opt/aaka-config")) / "data" / "queue" / "butler.db")
            with _sq.connect(_db_path) as _conn:
                _conn.row_factory = _sq.Row
                row = _conn.execute("SELECT * FROM queue_items WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1", (f"{prefix}%",)).fetchone()
            if not row:
                return _reply(f"{REPLY_PREFIX}❌ No queue item matching #{prefix}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            item = dict(row)
            item_id = item["id"]
            status = item["status"]
            intent_name = item["intent"]
            short = item_id[:8]
            if status == "cancelled":
                return _reply(f"{REPLY_PREFIX}Already cancelled. `#{short}`", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            if status in ("awaiting_confirm", "confirmed", "executing"):
                if not dry_run:
                    _q_update(item_id, "cancelled")
                    clear_pending_confirm(sender_id)
                return _reply(f"{REPLY_PREFIX}❌ Cancelled {intent_name} (was {status}). `#{short}`", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            if status == "done":
                result_raw = item.get("result") or "{}"
                result_data = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
                event_ids = result_data.get("event_ids", [])
                if event_ids and intent_name in ("add_event", "add_event_batch"):
                    if not dry_run:
                        _q_update(item_id, "cancelled", result={"undone": True, "original_event_ids": [e if isinstance(e, str) else e.get("id", "") for e in event_ids]})
                        try:
                            from skills.calendar.gog import delete_event
                            payload_data = json.loads(item.get("payload") or "{}") if isinstance(item.get("payload"), str) else (item.get("payload") or {})
                            cal_id = aaka_config.target_calendar_id(
                                payload_data.get("sender_member_id", ""), payload_data.get("calendar_tag", "")
                            )
                            deleted = 0
                            for eid in event_ids:
                                eid_str = eid if isinstance(eid, str) else eid.get("id", "")
                                if eid_str:
                                    try:
                                        delete_event(eid_str, calendar_id=cal_id)
                                        deleted += 1
                                    except Exception:
                                        pass
                            return _reply(f"{REPLY_PREFIX}↩️ Undone — deleted {deleted} event(s) from calendar. `#{short}`", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                        except Exception as exc:
                            return _reply(f"{REPLY_PREFIX}⚠️ Cancelled queue item but couldn't delete calendar events: {exc}\n`#{short}`", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                    return _reply(f"{REPLY_PREFIX}[dry-run] would undo {intent_name} #{short}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                if intent_name == "drop_file":
                    # Queue undo_drop for executor (vault is on Mac, sensor on VPS)
                    namespace = _namespace_for_sender(sender_id) or "user"
                    undo_payload = {
                        "original_item_id": item_id,
                        "namespace": namespace,
                        "message_id": message_id,
                        "source": source,
                    }
                    if not dry_run:
                        from aaka_queue.queue import write_item as _q_write, update_status as _q_up
                        undo_id = _q_write(
                            intent="undo_drop", raw_message=message, sender=sender_id,
                            channel_id=channel_id, source=source, payload=undo_payload,
                        )
                        _q_up(undo_id, "confirmed")
                    return _reply(f"{REPLY_PREFIX}↩️ Moving back to Inbox… `#{short}`", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                # Done but no event_ids to delete
                if not dry_run:
                    _q_update(item_id, "cancelled")
                return _reply(f"{REPLY_PREFIX}❌ Cancelled {intent_name}. `#{short}`", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            return _reply(f"{REPLY_PREFIX}⚠️ Item #{short} has status '{status}' — cannot undo.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        except Exception as exc:
            return _reply(f"{REPLY_PREFIX}⚠️ Undo failed: {exc}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── drop_note: sensor-side append + file_sync ──────────────────────────────
    #    First word = topic (log filename), rest = note content.
    #    "n fin 2025 claim boston taxi" → appends to data/notes/fin.md instantly
    if intent == "drop_note":
        from skills.notes.note_writer import append_note, sync_dest
        text = re.sub(r'^/note\s+', '', message, flags=re.I)
        if text == message:  # no /note prefix — shouldn't happen, but guard
            text = message
        text = text.strip()
        if not text:
            return _reply(f"{REPLY_PREFIX}Usage: n <topic> <text>\nExample: n fin 2025 claim boston taxi", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        # Parse topic (first token on first line) from body (everything after),
        # preserving newlines so multi-line input becomes multiple entries.
        _first_nl = text.find('\n')
        if _first_nl >= 0:
            _first_line_parts = text[:_first_nl].split(None, 1)
            _first_line_words = _first_line_parts[0].split() if _first_line_parts else []
            _rest_of_first = _first_line_parts[1] if len(_first_line_parts) > 1 else ""
            _multiline_rest = text[_first_nl + 1:]
        else:
            _first_line_words = text.split()
            _rest_of_first = ""
            _multiline_rest = ""

        # Smart Drop style: check if first word is a member name (cross-member note)
        _sender_namespace = _namespace_for_sender(sender_id) or (aaka_config.carriers()[0] if aaka_config.carriers() else "user")
        namespace = _sender_namespace
        _word_idx = 0
        _actor_member = aaka_config.member_by_name(_first_line_words[0].lower()) if _first_line_words else None
        if _actor_member and _actor_member["id"] != _sender_namespace:
            # Cross-member note — require admin
            if not aaka_config.member_is_admin(_sender_namespace):
                return _reply(f"{REPLY_PREFIX}Only admins can write notes to another member's vault.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            namespace = _actor_member["id"]
            _word_idx = 1
        elif _actor_member and _actor_member["id"] == _sender_namespace:
            # First word is own name — skip it (e.g. "n alex fin hello")
            _word_idx = 1

        remaining_words = _first_line_words[_word_idx:]
        topic = remaining_words[0].lower() if remaining_words else ""
        _body_start = " ".join(remaining_words[1:]) if len(remaining_words) > 1 else ""
        body_parts = [p for p in [_body_start, _rest_of_first, _multiline_rest] if p.strip()]
        body = " ".join(body_parts).strip()

        if not topic:
            return _reply(f"{REPLY_PREFIX}Usage: n <topic> <text>\nExample: n fin 2025 claim boston taxi", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        if not body:
            return _reply(f"{REPLY_PREFIX}Usage: n <topic> <text>  (to write)\n        n <topic>       (to read)", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # Parse !canonical-name alias from body (e.g. "!ari-ortho-treatment first visit")
        alias_name = None
        _alias_match = re.search(r'(?:^|\s)!([a-zA-Z0-9][a-zA-Z0-9_-]*)(?:\s|$)', body)
        if _alias_match:
            alias_name = _alias_match.group(1).lower()
            body = re.sub(r'\s*!([a-zA-Z0-9][a-zA-Z0-9_-]*)\s*', ' ', body).strip()

        # If media attached, compute drop filename and inline it into the note body
        drop_name = None
        if media is not None:
            import shutil
            import uuid
            from tools.file_utils import validate_extension, validate_size, sanitize_filename as _sanitize, generate_drop_filename as _gen_name
            media_path = Path(media["media_path"])
            ext = media_path.suffix.lower()
            if media_path.exists() and validate_extension(ext) and validate_size(media_path, max_mb=50):
                drop_name = _gen_name([topic], media["original_filename"])
                body += f" 📎 {drop_name}"

        if not dry_run:
            # Resolve route owner: if tag has a registered owner, notes go to their vault
            # even without specifying the member name (admin-only write still applies)
            _resolved_topic = alias_name if alias_name else topic
            try:
                from tools.inbox_router import load_sensor_references, resolve_topic_alias, resolve_route_owner
                _note_refs = load_sensor_references()
                _canonical = resolve_topic_alias(_resolved_topic, _note_refs)
                _route_owner = resolve_route_owner(_canonical, _note_refs)
                if _route_owner and _route_owner != namespace:
                    if not aaka_config.member_is_admin(_sender_namespace):
                        return _reply(f"{REPLY_PREFIX}Only admins can write notes to #{topic} (owned by {_route_owner}).", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                    namespace = _route_owner
            except Exception:
                pass
            _source_path, formatted_lines, entry_count = append_note(topic, body, namespace, alias_name=alias_name)
            dest_path = sync_dest(_resolved_topic, namespace)
            _upsert_id = write_item(
                intent="file_upsert", raw_message=f"note upsert {topic}",
                sender=sender_id, channel_id=channel_id, source=source,
                payload={
                    "topic": topic,
                    "lines": formatted_lines,
                    "namespace": namespace,
                    "dest_vault_path": dest_path,
                    "entry_count_after": entry_count,
                },
            )
            update_status(_upsert_id, "confirmed")
        else:
            print(f"[dry-run] intent=drop_note  topic={topic} body={body!r}")
        # Strip URLs from preview so Telegram doesn't unfurl link previews
        _preview_body = re.sub(r'https?://\S+', '[link]', body)
        _body_preview = _preview_body.replace('\n', ' · ')[:80]
        reply_parts = [f"📝 #{topic} — {_body_preview}{'…' if len(_preview_body) > 80 else ''}\n\n↪ `n {topic}` to read · `n {topic} <text>` to add"]

        # Stage the file for executor to move to vault
        if drop_name is not None:
            config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
            staging_id = uuid.uuid4().hex[:12]
            staging_dir = config_dir / "data" / "staging" / staging_id
            safe_name = _sanitize(media["original_filename"])
            if not dry_run:
                staging_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(media_path), str(staging_dir / safe_name))
            file_payload = {
                "tags": [topic],
                "media_staging_path": f"staging/{staging_id}/{safe_name}",
                "mime_type": media["mime_type"],
                "original_filename": media["original_filename"],
                "file_size_bytes": media_path.stat().st_size,
                "caption": body,
                "namespace": namespace,
                "message_id": message_id,
                "source": source,
            }
            if not dry_run:
                file_item_id = write_item(
                    intent="drop_file", raw_message=message, sender=sender_id,
                    channel_id=channel_id, source=source, payload=file_payload,
                )
                update_status(file_item_id, "confirmed")
            reply_parts.append(f"📎 + file staged: {drop_name}")

        return _reply(f"{REPLY_PREFIX}" + "\n".join(reply_parts), channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── drop_ask: bare attachment → stage + prompt user for tags ─────────────
    if intent == "drop_ask":
        import shutil
        import uuid
        from tools.file_utils import validate_extension, validate_size, sanitize_filename

        if media is None:
            return _reply(f"{REPLY_PREFIX}📎 No attachment found.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        media_path = Path(media["media_path"])
        if not media_path.exists():
            return _reply(f"{REPLY_PREFIX}⚠️ Media file not found: {media_path.name}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        ext = media_path.suffix.lower()
        if not validate_extension(ext):
            return _reply(f"{REPLY_PREFIX}⚠️ File type '{ext}' is not allowed.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        if not validate_size(media_path, max_mb=50):
            return _reply(f"{REPLY_PREFIX}⚠️ File exceeds 50 MB size limit.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # Stage file immediately so it isn't lost
        config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
        staging_id = uuid.uuid4().hex[:12]
        staging_dir = config_dir / "data" / "staging" / staging_id
        safe_name = sanitize_filename(media["original_filename"])

        if not dry_run:
            staging_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(media_path), str(staging_dir / safe_name))

        file_size = media_path.stat().st_size
        size_kb = file_size // 1024

        # Suggest tags from existing note topics
        notes_dir = aaka_config.DATA_DIR / "notes"
        suggested = sorted(p.stem for p in notes_dir.glob("*.md")) if notes_dir.exists() else []
        suggested = suggested[:8]

        if not dry_run:
            set_pending_confirm(sender_id, f"drop-ask:{staging_id}")

        prompt = f"📎 Received *{safe_name}* ({size_kb} KB).\nDrop it? Reply with tags, or *skip*."
        if suggested:
            prompt += "\nSuggested: " + " ".join(f"#{t}" for t in suggested)

        # Inline keyboard buttons (Telegram only)
        if source == "telegram" and suggested:
            rows = []
            row = []
            for tag in suggested:
                row.append({"text": f"#{tag}", "callback_data": f"drop-tag:{staging_id}:{tag}"})
                if len(row) >= 4:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([{"text": "Skip ⏭", "callback_data": f"drop-skip:{staging_id}"}])
            _markup = json.dumps({"inline_keyboard": rows})
            prompt += f"\n__MARKUP__:{_markup}"

        return _reply(f"{REPLY_PREFIX}{prompt}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── drop_file: /drop <tags> or media auto-detect → stage + queue ─────────
    if intent == "drop_file":
        import shutil
        import uuid
        from tools.file_utils import validate_extension, validate_size, sanitize_filename
        from tools.inbox_router import known_areas_for

        # ── Parse enhanced Smart Drop caption ─────────────────────────────────
        # Supports: f ari health #ortho "260507-doc". description text here
        #           f ari tax "invoice". Finanzamt 2025 annual
        # Before "." = routing + naming.  After "." = description for files.md
        caption = re.sub(r'^/drop\s*', '', message, flags=re.I).strip()

        # 0. Extract quoted custom name first (before dot split)
        _qm = re.search(r'"([^"]+)"', caption)
        custom_name = _qm.group(1).strip() if _qm else None
        _caption_no_quotes = re.sub(r'"[^"]*"', '', caption).strip() if _qm else caption

        # 1. Split on first ". " (dot+space) to separate routing from description
        description = ""
        _dot_idx = _caption_no_quotes.find(". ")
        if _dot_idx >= 0:
            routing_stripped = _caption_no_quotes[:_dot_idx].strip()
            description = _caption_no_quotes[_dot_idx + 2:].strip()
        elif _caption_no_quotes.endswith("."):
            routing_stripped = _caption_no_quotes[:-1].strip()
        else:
            routing_stripped = _caption_no_quotes

        # 2. Extract #hash_tags (subfolder-create tags)
        hash_tags = []
        _remaining_words = []
        for _w in routing_stripped.split():
            if _w.startswith('#') and len(_w) > 1:
                hash_tags.append(_w[1:].lower())
            else:
                _remaining_words.append(_w)

        # 3. Check for skip-compress keywords
        _SKIP_COMPRESS_KEYWORDS = {
            "keep", "original", "og", "nocompress", "no-compress", "no_compress"
        }
        skip_compress = False
        clean_words = []
        for _w in _remaining_words:
            if _w.lower() in _SKIP_COMPRESS_KEYWORDS:
                skip_compress = True
            else:
                clean_words.append(_w)

        # 4. Check first word for member actor
        actor = None
        _sender_namespace = _namespace_for_sender(sender_id) or (aaka_config.carriers()[0] if aaka_config.carriers() else "user")
        if clean_words:
            _first = clean_words[0]
            _member = aaka_config.member_by_name(_first)
            if _member:
                actor = _member["id"]
                clean_words = clean_words[1:]

        # 5. Check next word for known area (dynamically scanned from vault)
        area = None
        _actor_id = actor or _sender_namespace
        _valid_areas = known_areas_for(_actor_id)
        if clean_words and clean_words[0].lower() in _valid_areas:
            area = clean_words[0].lower()
            clean_words = clean_words[1:]

        # 6. Remaining words are regular tags (routing keywords checked by executor)
        tags = [_w.lower() for _w in clean_words if re.match(r'^[a-z0-9_-]+$', _w.lower()) and len(_w) <= 20]

        namespace = actor or _sender_namespace

        # Admin gate: only admins can drop files into another member's vault
        if actor and actor != _sender_namespace and not aaka_config.member_is_admin(_sender_namespace):
            return _reply(f"{REPLY_PREFIX}Only admins can drop files into another member's vault.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        if media is None:
            return _reply(f"{REPLY_PREFIX}📎 /drop requires a file or photo attachment.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        media_path = Path(media["media_path"])
        if not media_path.exists():
            return _reply(f"{REPLY_PREFIX}⚠️ Media file not found: {media_path.name}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # Validate extension
        ext = media_path.suffix.lower()
        if not validate_extension(ext):
            return _reply(f"{REPLY_PREFIX}⚠️ File type '{ext}' is not allowed.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # Validate size
        if not validate_size(media_path, max_mb=50):
            return _reply(f"{REPLY_PREFIX}⚠️ File exceeds 50 MB size limit.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        # Stage: copy to /config/data/staging/<uuid>/<sanitized_filename>
        config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
        staging_id = uuid.uuid4().hex[:12]
        staging_dir = config_dir / "data" / "staging" / staging_id
        safe_name = sanitize_filename(media["original_filename"])
        staging_dest = staging_dir / safe_name

        if not dry_run:
            staging_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(media_path), str(staging_dest))

        file_size = media_path.stat().st_size
        staging_rel = f"staging/{staging_id}/{safe_name}"

        # file_count: Telegram media groups arrive as separate messages, so
        # we always have 1 file per invocation. Caller can override via caption.
        file_count = 1

        # Legacy path (no actor) only reads `tags` for routing — merge any
        # caption hash_tags in so a bare "#den40" actually routes through
        # references.yaml instead of being silently dropped on the floor.
        _payload_tags = list(tags)
        _payload_hash_tags = list(hash_tags)
        if not actor:
            for h in hash_tags:
                if h not in _payload_tags:
                    _payload_tags.append(h)
            _payload_hash_tags = []

        payload = {
            "tags": _payload_tags,
            "media_staging_path": staging_rel,
            "mime_type": media["mime_type"],
            "original_filename": media["original_filename"],
            "file_size_bytes": file_size,
            "caption": caption,
            "description": description,
            "namespace": namespace,
            "message_id": message_id,
            "source": source,
            # Smart Drop keys (None/False/[] means legacy path in executor)
            "actor": actor,
            "area": area,
            "hash_tags": _payload_hash_tags,
            "custom_name": custom_name,
            "skip_compress": skip_compress,
            "file_count": file_count,
        }
        if not dry_run:
            item_id = write_item(
                intent="drop_file", raw_message=message, sender=sender_id,
                channel_id=channel_id, source=source, payload=payload,
            )
            update_status(item_id, "confirmed")
            hash_line = f"\n`#{item_id[:8]}`"
        else:
            print(f"[dry-run] intent=drop_file  payload={json.dumps(payload, indent=2)}")
            hash_line = ""

        # Build sensor preview
        from skills.drop.format import render_smart_drop_preview, render_legacy_drop_preview
        if actor:
            preview_msg = render_smart_drop_preview(
                safe_name=safe_name, actor=actor, area=area,
                hash_tags=hash_tags, custom_name=custom_name,
                skip_compress=skip_compress, hash_line=hash_line,
            )
        else:
            from tools.inbox_router import preview_destination
            size_kb = file_size // 1024
            # Legacy executor only routes via `tags`. Merge any caption hash_tags
            # so a bare "#den40" still gets a destination preview AND, when we
            # patch executor, routes correctly. Until then we at least preview
            # accurately by passing the merged list.
            _route_tags = list(tags) + [h for h in hash_tags if h not in tags]
            dest = preview_destination(_route_tags, namespace)
            preview_msg = render_legacy_drop_preview(
                safe_name=safe_name, size_kb=size_kb,
                tags=_route_tags, destination=dest, hash_line=hash_line,
            )
        return _reply(f"{REPLY_PREFIX}{preview_msg}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── plan_slots: direct execution on sensor (VPS calendar token) ─────────────
    if intent == "plan_slots":
        _plan_args = re.sub(r'^/plan\s*', '', message, flags=re.I).strip()
        if not _plan_args:
            from skills.calendar.plan import describe as _plan_describe
            return _reply(f"{REPLY_PREFIX}{_plan_describe()}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        from skills.calendar.plan import parse_plan_args, find_plan_slots, format_plan_reply
        try:
            _args = parse_plan_args(_plan_args)
            _available, _reserved = find_plan_slots(
                dates=_args["dates"],
                duration_minutes=_args["duration_minutes"],
                time_window=_args["time_window"],
                use_work_hours=_args["use_work_hours"],
                ignore_reserved=_args["ignore_reserved"],
                tags=_args.get("tags"),
            )
            _reply_text = format_plan_reply(
                dates=_args["dates"],
                available=_available[:5] if len(_available) > 5 else _available,
                reserved=_reserved,
                duration_minutes=_args["duration_minutes"],
                recap=_args.get("recap", ""),
            )
            if len(_available) > 5:
                _reply_text += f"\n(+{len(_available) - 5} more — check Google Calendar)"
            return _reply(f"{REPLY_PREFIX}{_reply_text}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        except Exception as _plan_exc:
            return _reply(f"{REPLY_PREFIX}⚠️ /plan failed: {_plan_exc}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── day_schedule: direct execution on sensor (VPS calendar token) ───────────
    if intent == "day_schedule":
        from skills.calendar.plan import parse_day_date
        from skills.calendar.availability import fetch_day_events
        _day_args = re.sub(r'^/day\s*', '', message, flags=re.I).strip()
        try:
            _target_date = parse_day_date(_day_args)
            _cal_ids = aaka_config.all_calendar_ids()
            _events = fetch_day_events(_cal_ids, _target_date.isoformat())
            _date_label = _target_date.strftime("%a %-d %b")
            if not _events:
                _day_text = f"📅 {_date_label} — nothing on the calendar."
            else:
                _day_lines = [f"📅 {_date_label}"]
                for _ev in _events:
                    if _ev["all_day"]:
                        _day_lines.append(f"  • {_ev['title']}")
                    else:
                        _day_lines.append(f"  • {_ev['start']}–{_ev['end']}  {_ev['title']}")
                _day_text = "\n".join(_day_lines)
            return _reply(f"{REPLY_PREFIX}{_day_text}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        except Exception as _day_exc:
            return _reply(f"{REPLY_PREFIX}⚠️ /day failed: {_day_exc}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── add_task: direct local JSON write ────────────────────────────────────────
    if intent == "add_task":
        sender_member = aaka_config.member_by_sender(sender_id)
        sender_mid_id = sender_member["id"] if sender_member else ""
        try:
            payload = _extract_task(message, sender_member_id=sender_mid_id)
        except Exception as exc:
            return _reply(f"{REPLY_PREFIX}⚠️ Could not parse task: {exc}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        payload["agent"] = "aaka"
        if dry_run:
            print(f"[dry-run] intent=add_task  payload={json.dumps(payload, ensure_ascii=False)}")
            return ""
        from skills.tasks.local_tasks import add_task as _add_task
        task = _add_task(payload)
        title = task["title"]
        due = task.get("due_date", "")
        due_str = f"  📅 {due}" if due else ""
        return _reply(f"{REPLY_PREFIX}📝 Added: {title}{due_str}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── complete_task: direct local JSON write ────────────────────────────────
    if intent == "complete_task":
        raw = _extract_complete_task(message)
        # History view: /done today, /done week
        if raw.get("history"):
            from skills.tasks.local_tasks import list_completed as _list_completed
            since = raw["history"]
            done = _list_completed(since=since)
            if not done:
                return _reply(f"{REPLY_PREFIX}No tasks completed {since}.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            lines = [f"✅ Completed {since} ({len(done)})\n"]
            for t in done:
                lines.append(f"  ✓ {t.get('title', '')}")
            return _reply(f"{REPLY_PREFIX}" + "\n".join(lines), channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        payload = _resolve_complete_task_payload(raw)
        if dry_run:
            print(f"[dry-run] intent=complete_task  payload={json.dumps(payload, ensure_ascii=False)}")
            return ""
        # Bulk completion: /done 1 3 5
        if payload.get("bulk"):
            from skills.tasks.local_tasks import complete_tasks_by_nums, recreate_recurring
            from skills.tasks.format import render_completed
            try:
                completed = complete_tasks_by_nums(payload["task_nums"])
                if not completed:
                    return _reply(f"{REPLY_PREFIX}⚠️ No matching tasks found.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                recurring_next = []
                for t in completed:
                    if t.get("recurring"):
                        new_t = recreate_recurring(t)
                        if new_t:
                            recurring_next.append(new_t)
                return _reply(f"{REPLY_PREFIX}" + render_completed(completed, recurring_next), channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            except Exception as exc:
                return _reply(f"{REPLY_PREFIX}⚠️ {exc}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        # Single task completion
        if payload.get("task_id"):
            from skills.tasks.local_tasks import complete_task_by_id as _complete_one, recreate_recurring
            from skills.tasks.format import render_completed
            try:
                t = _complete_one(payload["task_id"])
                t = dict(t)
                if not t.get("title"):
                    t["title"] = payload.get("title_hint", "task")
                recurring_next = []
                if t.get("recurring"):
                    new_t = recreate_recurring(t)
                    if new_t:
                        recurring_next.append(new_t)
                return _reply(f"{REPLY_PREFIX}" + render_completed([t], recurring_next), channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            except Exception as exc:
                return _reply(f"{REPLY_PREFIX}⚠️ {exc}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        hint = payload.get("title_hint", "")
        return _reply(f"{REPLY_PREFIX}⚠️ Couldn't find task{f': {hint!r}' if hint else ''}. Use /tasks first, then /done N.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── Queue-bound intents ───────────────────────────────────────────────────
    if intent not in _QUEUE_INTENTS:
        return _reply(f"{REPLY_PREFIX}⚙️ {intent} is not yet supported on the sensor node.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── Bare-command guards: show help when required args are missing ─────────────
    def _bare(cmd_prefix: str) -> bool:
        stripped = re.sub(rf'^{cmd_prefix}\s*', '', message, flags=re.I).strip()
        return re.sub(r'(?i)\s*#me\b', '', stripped).strip() == ""

    # ── edit_event: "/cal edit <search>" or "/cal edit <N> <changes>" ──────────
    if intent == "edit_event":
        from skills.calendar import edit_event as _edit

        # Strip command prefix → either "/cal edit ..." or "/edit event ..."
        body = re.sub(r'^/(?:cal\s+edit|edit\s+event)\s*', '', message, flags=re.I).strip()

        if not body:
            return _reply(
                f"{REPLY_PREFIX}Usage: /cal edit <search> — find an event\n"
                f"        /cal edit <N> <changes> — apply changes to candidate N\n\n"
                f"Changes: + @alex · time 18:00-20:00 · date 2026-06-20\n"
                f"         title: New · loc: New · desc: New · private · no-email",
                channel_id=channel_id, sender=sender_id, message_id=message_id,
                dry_run=dry_run, source=source,
            )

        sender_member = aaka_config.member_by_sender(sender_id)
        sender_mid_id = sender_member["id"] if sender_member else ""
        if not sender_mid_id:
            return _reply(f"{REPLY_PREFIX}🔒 Unrecognised sender.",
                          channel_id=channel_id, sender=sender_id, message_id=message_id,
                          dry_run=dry_run, source=source)

        # Apply path: starts with digit
        num_m = re.match(r'^(\d+)\b\s*(.*)$', body, re.S)
        if num_m:
            num = int(num_m.group(1))
            change_text = num_m.group(2).strip()
            item = _edit.load_edit_item(sender_id, num)
            if not item:
                return _reply(f"{REPLY_PREFIX}Edit item #{num} not found. Run `/cal edit <search>` first.",
                              channel_id=channel_id, sender=sender_id, message_id=message_id,
                              dry_run=dry_run, source=source)
            if not change_text:
                # Show current state of the chosen event
                when = _edit._format_when(item.get("start", ""), item.get("end", ""))
                attendees = ", ".join(item.get("attendees", [])) or "(none)"
                detail = (
                    f"📅 {item.get('title','')}\n"
                    f"   {when}\n"
                    f"   📍 {item.get('location','') or '(no location)'}\n"
                    f"   👥 {attendees}\n\n"
                    f"Reply: /cal edit {num} <changes>"
                )
                return _reply(f"{REPLY_PREFIX}{detail}",
                              channel_id=channel_id, sender=sender_id, message_id=message_id,
                              dry_run=dry_run, source=source)

            changes = _edit.parse_modifiers(change_text, sender_mid_id)
            errors = changes.pop("errors", [])
            has_change = any(k in changes for k in (
                "title", "description", "location", "add_guests", "visibility",
                "date", "start_time", "end_time", "send_updates",
            ))
            if not has_change:
                err = "\n⚠️ " + "\n⚠️ ".join(errors) if errors else ""
                return _reply(
                    f"{REPLY_PREFIX}No recognised changes in: '{change_text}'.{err}\n"
                    f"Try: `+ @alex` · `time 18:00-20:00` · `loc: New` · `title: New`",
                    channel_id=channel_id, sender=sender_id, message_id=message_id,
                    dry_run=dry_run, source=source,
                )

            if dry_run:
                print(f"[dry-run] edit_event #{num}  changes={changes}")
                return ""

            if _VPS_TOKEN.exists():
                try:
                    _edit.apply_changes(item, changes, token_file=_VPS_TOKEN)
                    summary = _edit.format_change_summary(changes)
                    warn = ("\n⚠️ " + "\n⚠️ ".join(errors)) if errors else ""
                    return _reply(f"{REPLY_PREFIX}✅ Updated: {item.get('title','')}\n   {summary}{warn}",
                                  channel_id=channel_id, sender=sender_id, message_id=message_id,
                                  dry_run=dry_run, source=source)
                except Exception as _edit_exc:
                    return _reply(f"{REPLY_PREFIX}⚠️ Edit failed: {_edit_exc}",
                                  channel_id=channel_id, sender=sender_id, message_id=message_id,
                                  dry_run=dry_run, source=source)
            # No VPS token → queue (executor handles it). Minimal payload.
            payload = {
                "event_id":    item["event_id"],
                "calendar_id": item.get("calendar_id", ""),
                "changes":     changes,
                "title":       item.get("title", ""),
                "message_id":  message_id,
                "source":      source,
                "channel_id":  channel_id,
            }
            item_id = write_item(intent="edit_event", raw_message=message,
                                 sender=sender_id, channel_id=channel_id,
                                 source=source, payload=payload)
            update_status(item_id, "confirmed")
            return _reply(f"{REPLY_PREFIX}⏳ Updating event…",
                          channel_id=channel_id, sender=sender_id, message_id=message_id,
                          dry_run=dry_run, source=source)

        # Search path
        if dry_run:
            print(f"[dry-run] edit_event search='{body}'")
            return ""
        token = _VPS_TOKEN if _VPS_TOKEN.exists() else None
        try:
            candidates = _edit.search_candidates(body, sender_mid_id, token_file=token)
        except Exception as _search_exc:
            return _reply(f"{REPLY_PREFIX}⚠️ Search failed: {_search_exc}",
                          channel_id=channel_id, sender=sender_id, message_id=message_id,
                          dry_run=dry_run, source=source)
        if candidates:
            _edit.save_edit_list(sender_id, candidates)
        return _reply(f"{REPLY_PREFIX}{_edit.format_candidate_list(candidates, body)}",
                      channel_id=channel_id, sender=sender_id, message_id=message_id,
                      dry_run=dry_run, source=source)

    # ── fix_event: assign carrier via "c fix N <name>" ─────────────────────────
    if intent == "fix_event":
        # Normalise: strip "#" so both "c fix" and "c #fix" work uniformly
        _fix_msg = re.sub(r'#fix\b', 'fix', message, flags=re.I)
        fix_match = re.search(r'fix\s+(\d+)\s+(\w+)', _fix_msg, re.I)
        if not fix_match:
            # "c fix N" with no carrier name → show event detail card
            num_only = re.search(r'fix\s+(\d+)\s*$', _fix_msg, re.I)
            if num_only:
                from skills.calendar.fix_analyzer import load_fix_item
                item = load_fix_item(sender_id, int(num_only.group(1)))
                if not item:
                    return _reply(f"{REPLY_PREFIX}Fix item #{num_only.group(1)} not found. Run w first to refresh.",
                                  channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                return _reply(_format_fix_detail(item),
                              channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            # Bare "c fix" / "c #fix" → show fix list for the week
            from skills.calendar.fix_analyzer import analyze_fix, format_fix_list, save_fix_list
            mid = _namespace_for_sender(sender_id)
            if not mid:
                return _reply(f"{REPLY_PREFIX}🔒 Fix access requires a recognised sender.",
                              channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            issues = analyze_fix("week", member_id=mid)
            if sender_id and issues:
                save_fix_list(sender_id, issues)
            body = format_fix_list(issues)
            body += "\n\n💡 c fix <filter>  where filter: carrier · overlap · ghosted"
            return _reply(body, channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        fix_num = int(fix_match.group(1))
        fix_action = fix_match.group(2)  # carrier name OR #work
        # Detect #work modifier: "c fix 1 #work" or "c fix 1 #work Alex"
        _work_flag = bool(re.search(r'#work\b', message, re.I))
        # Extract carrier name and optional ad-hoc email (skip #work token)
        _fix_tail = re.sub(r'#work\s*', '', _fix_msg[fix_match.start(2):], flags=re.I).strip()
        _fix_tokens = _fix_tail.split()
        carrier_name = _fix_tokens[0].capitalize() if _fix_tokens else ""
        # Optional second token: ad-hoc email for non-registered carriers
        _adhoc_email_re = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
        _adhoc_email = _fix_tokens[1] if len(_fix_tokens) > 1 and _adhoc_email_re.match(_fix_tokens[1]) else ""
        from skills.calendar.fix_analyzer import load_fix_item
        item = load_fix_item(sender_id, fix_num)
        if not item:
            return _reply(f"{REPLY_PREFIX}Fix item #{fix_num} not found. Run w first to refresh.",
                          channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        # #work without carrier name → just add work email + private (any issue type)
        if _work_flag and not carrier_name:
            sender_member = aaka_config.member_by_sender(sender_id)
            if not sender_member:
                return _reply(f"{REPLY_PREFIX}🔒 Unrecognised sender.",
                              channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            work_email = (sender_member.get("calendars", {}).get("work") or {}).get("id", "")
            if not work_email:
                return _reply(f"{REPLY_PREFIX}No work calendar configured for {sender_member['name']}.",
                              channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            if _VPS_TOKEN.exists() and not dry_run:
                try:
                    from skills.calendar.gog import update_event as _update_event
                    _update_event(
                        event_id=item.get("event_id", ""),
                        calendar_id=item.get("calendar_id", ""),
                        add_guests=[work_email],
                        visibility="private",
                        token_file=_VPS_TOKEN,
                    )
                    return _reply(f"{REPLY_PREFIX}✅ Added to work calendar (private).\n🔒 {item.get('title', '')}",
                                  channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
                except Exception as _wk_exc:
                    return _reply(f"{REPLY_PREFIX}⚠️ Work flag failed: {_wk_exc}",
                                  channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            # Queue fallback
            payload = {
                "event_id": item.get("event_id", ""),
                "calendar_id": item.get("calendar_id", ""),
                "title": item.get("title", ""),
                "work_email": work_email,
                "visibility": "private",
                "message_id": message_id,
                "source": source,
                "channel_id": channel_id,
            }
            if not dry_run:
                item_id = write_item(intent="fix_event", raw_message=message,
                                     sender=sender_id, channel_id=channel_id,
                                     source=source, payload=payload)
                update_status(item_id, "confirmed")
            return _reply(f"{REPLY_PREFIX}⏳ Adding to work calendar (private)…",
                          channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        # Carrier assignment path
        if not carrier_name:
            carrier_name = fix_action.capitalize()
        if item.get("type") != "carrier_missing":
            return _reply(f"{REPLY_PREFIX}Item #{fix_num} is a {item['type']} — carrier assignment only works for missing-carrier issues.\n💡 Try: c fix {fix_num} #work",
                          channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        _is_known_carrier = carrier_name.lower() in [c.lower() for c in aaka_config.carriers()]
        if _is_known_carrier:
            carrier_emails = aaka_config.guest_emails_for(carrier_name)
            _adhoc_note = ""
        else:
            # Ad-hoc carrier (babysitter, neighbour, etc.) — allowed with a warning
            carrier_emails = [_adhoc_email] if _adhoc_email else []
            known = ", ".join(aaka_config.carriers())
            _adhoc_note = f"\n⚠️ '{carrier_name}' is not a registered carrier (known: {known})."
            if not _adhoc_email:
                _adhoc_note += " No email provided — title updated but no invite sent."
        # If #work flag with carrier: also add work email + private
        _extra_guests = []
        _visibility = None
        if _work_flag:
            sender_member = aaka_config.member_by_sender(sender_id)
            if sender_member:
                work_email = (sender_member.get("calendars", {}).get("work") or {}).get("id", "")
                if work_email:
                    _extra_guests.append(work_email)
                    _visibility = "private"
        payload = {
            "event_id": item.get("event_id", ""),
            "calendar_id": item.get("calendar_id", ""),
            "title": item.get("title", ""),
            "attendee": item.get("attendee", ""),
            "carrier_name": carrier_name,
            "carrier_email": carrier_emails[0] if carrier_emails else "",
            "adhoc_carrier": not _is_known_carrier,
            "adhoc_email": _adhoc_email,
            "message_id": message_id,
            "source": source,
            "channel_id": channel_id,
        }
        # ── Direct VPS execution (no queue, instant) ───────────────────────────
        if _VPS_TOKEN.exists() and not dry_run:
            try:
                from skills.calendar.gog import update_event as _update_event
                old_title = payload.get("title", "")
                if '🤝' in old_title and '❓' in old_title:
                    new_title = re.sub(r'🤝\s*❓', f'🤝 {carrier_name}', old_title)
                else:
                    _att = payload.get("attendee", "")
                    if not _att:
                        for _n in aaka_config.needs_carrier():
                            if re.search(rf'\b{re.escape(_n)}\b', old_title, re.I):
                                _att = _n
                                break
                    if _att and re.search(rf'\b{re.escape(_att)}\b', old_title, re.I):
                        new_title = re.sub(rf'(\b{re.escape(_att)}\b)', rf'\1 (🤝 {carrier_name})', old_title, count=1, flags=re.I)
                    else:
                        new_title = f"{old_title} (🤝 {carrier_name})"
                all_guests = (carrier_emails[:1] if carrier_emails else []) + _extra_guests
                # For ad-hoc carriers, append their name (and email) to the description
                _desc_update = None
                if not _is_known_carrier:
                    _existing_desc = item.get("description", "")
                    _carrier_line = f"carrier: {carrier_name}"
                    if _adhoc_email:
                        _carrier_line += f" <{_adhoc_email}>"
                    _desc_update = f"{_existing_desc}\n{_carrier_line}".strip() if _existing_desc else _carrier_line
                _update_event(
                    event_id=payload["event_id"],
                    calendar_id=payload.get("calendar_id", ""),
                    title=new_title,
                    add_guests=all_guests,
                    visibility=_visibility,
                    description=_desc_update,
                    token_file=_VPS_TOKEN,
                )
                work_note = " 🔒+work" if _work_flag else ""
                return _reply(f"{REPLY_PREFIX}✅ {carrier_name} assigned as carrier.{work_note}\n📅 {new_title}{_adhoc_note}",
                              channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            except Exception as _fix_exc:
                logging.getLogger("sensor").warning(
                    "fix_event direct failed (%s) — falling back to queue", _fix_exc
                )
                # Fall through to queue path
        if not dry_run:
            item_id = write_item(intent="fix_event", raw_message=message,
                                 sender=sender_id, channel_id=channel_id,
                                 source=source, payload=payload)
            update_status(item_id, "confirmed")
        return _reply(f"{REPLY_PREFIX}Assigning {carrier_name} as carrier for: {item['title']}\n⏳ Updating calendar…{_adhoc_note}",
                      channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    if intent == "add_event" and _bare(r"/(cal|add)"):
        from skills.calendar.prepare_event import describe as _cal_describe
        return _reply(f"{REPLY_PREFIX}{_cal_describe()}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── linkedin_post: /li [YYYY-MM-DD HH:MM] <text> ──────────────────────────
    if intent == "linkedin_post":
        import uuid as _uuid
        from aaka_queue.queue import write_approval_item

        # Strip command prefix
        text = re.sub(r'^/li(?:nkedin)?\s+', '', message, flags=re.I).strip()

        # Parse optional schedule: [2026-05-12 09:00] or [tomorrow 9am] not supported yet — ISO only
        schedule_at = None
        sched_match = re.match(r'^\[(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)\]\s*', text)
        if sched_match:
            from datetime import datetime as _dt, timezone as _tz
            raw_dt = sched_match.group(1).strip()
            try:
                if len(raw_dt) == 10:
                    raw_dt += " 09:00"
                schedule_at = _dt.strptime(raw_dt, "%Y-%m-%d %H:%M").replace(tzinfo=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                text = text[sched_match.end():]
            except ValueError:
                pass

        if not text:
            return _reply(f"{REPLY_PREFIX}Usage: /li [YYYY-MM-DD HH:MM] Post text here\nOptionally attach an image.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

        approval_id = str(_uuid.uuid4())
        payload_li = {
            "text": text,
            "schedule_at": schedule_at,
            "message_id": message_id,
            "source": source,
            "approval_id": approval_id,
        }

        # Build preview
        preview_lines = ["📤 *LinkedIn post preview:*\n", text]
        if schedule_at:
            preview_lines.append(f"\n⏰ Scheduled: {schedule_at[:16].replace('T', ' ')} UTC")
        preview_lines.append("\nApprove to post, or cancel.")
        preview = "\n".join(preview_lines)

        if dry_run:
            print(f"[dry-run] linkedin_post: {text[:80]}")
            return ""

        item_id = write_approval_item(
            intent="linkedin_post",
            raw_message=message,
            sender=sender_id,
            channel_id=channel_id,
            source=source,
            payload=payload_li,
            approval_id=approval_id,
            schedule_at=schedule_at,
        )
        set_pending_confirm(sender_id, item_id)

        preview = f"{preview}\n`#{item_id[:8]}`"
        if source == "telegram":
            _markup = json.dumps({"inline_keyboard": [[
                {"text": "Post ✅", "callback_data": "yes"},
                {"text": "Cancel ❌", "callback_data": "cancel"},
            ]]})
            preview = f"{preview}\n__MARKUP__:{_markup}"
        return _reply(f"{REPLY_PREFIX}{preview}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── confirm_approval: re-send approval buttons for an awaiting item ─────────
    if intent == "confirm_approval":
        hash_match = re.search(r'#?([0-9a-f]{6,})', message, re.I)
        if not hash_match:
            return _reply(f"{REPLY_PREFIX}Usage: /confirm #abc12345", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        payload = {"hash": hash_match.group(1).lower(), "sender": sender_id, "channel_id": channel_id, "message_id": message_id}
        if not dry_run:
            item_id = write_item(intent="confirm_approval", raw_message=message, sender=sender_id, channel_id=channel_id, source=source, payload=payload)
            update_status(item_id, "confirmed")
        return _reply(f"{REPLY_PREFIX}⏳ Re-sending approval…", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # ── mail_fetch / mail_view / pw_manage: write directly as confirmed ──────────
    if intent in ("mail_fetch", "mail_view", "pw_manage"):
        payload = {"message": message, "message_id": message_id}
        if not dry_run:
            item_id = write_item(
                intent=intent, raw_message=message, sender=sender_id,
                channel_id=channel_id, source=source, payload=payload,
            )
            update_status(item_id, "confirmed")
        else:
            print(f"[dry-run] intent={intent}  payload={payload}")
        if intent == "mail_fetch":
            return _reply(f"{REPLY_PREFIX}⏳ Fetching mail…", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
        return _reply(f"{REPLY_PREFIX}⏳ Queued for executor…", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # Dedup check
    if not dry_run and is_duplicate(intent, message, sender_id):
        return _reply(f"{REPLY_PREFIX}⚠️ A similar request is already queued.", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    # Strip #me flag before LLM extraction (handled per-intent below)
    message, _me_flag = _strip_me_flag(message)

    # Extract payload via LLM
    try:
        if intent == "add_event":
            sender_member = aaka_config.member_by_sender(sender_id)
            sender_mid_id = sender_member["id"] if sender_member else ""
            events = _extract_events_batch(message, sender_email=sender_email,
                                           sender_member_id=sender_mid_id)
            # Per-event conflict check
            try:
                from skills.calendar.availability import conflicts_in_window
                for ev in events:
                    conflicts, checked_labels = conflicts_in_window(
                        ev.get("occurrences", []),
                        sender_mid_id,
                        extra_member_ids=ev.get("conflict_members", []),
                        blocker_members=[ev.get("attendee", ""), ev.get("carrier", "")],
                    )
                    ev["conflicts"] = conflicts
                    ev["checked_calendar_labels"] = checked_labels
            except Exception:
                pass
            # #me flag: force attendee to the sender on all events
            if _me_flag and sender_mid_id:
                sender_name = (aaka_config.member_by_sender(sender_id) or {}).get("name", sender_mid_id)
                for ev in events:
                    ev["attendee"] = sender_name
                    ev["carrier"] = ""
            if not events:
                return _reply(f"{REPLY_PREFIX}❌ Could not parse any events from that message. Try rephrasing or use a simpler format:\n`/cal Rumi Camp 10 July 2026`", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
            if len(events) == 1:
                # Single event — use existing single-event flow
                intent = "add_event"
                payload = events[0]
                payload["message_id"] = message_id
                preview = _format_event_preview(payload)
            else:
                # Batch — store as add_event_batch
                intent = "add_event_batch"
                from skills.calendar.prepare_event import format_batch_review
                payload = {"events": events, "message_id": message_id}
                preview = format_batch_review(events)
        elif intent == "block_cal":
            from skills.calendar.block import parse_block_args, build_blocks, format_block_preview
            from skills.calendar.availability import find_slots
            block_args = parse_block_args(message)
            dates = block_args["dates"]
            import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI
            import aaka_config as _ac
            tz_str = _ac.timezone()
            tz = _ZI(tz_str)
            cal_ids = _ac.all_calendar_ids()
            free_slots = find_slots(
                cal_ids,
                start_date=min(dates),
                end_date=max(dates),
                duration_minutes=10,
                time_window=("09:00", "17:00"),
                allowed_days={d.strftime("%A").lower() for d in dates},
                timezone=tz_str,
            )
            blocks = build_blocks(free_slots)
            # Store blocks as serialisable list for executor
            payload = {
                "dates": [d.isoformat() for d in dates],
                "recap": block_args.get("recap", ""),
                "blocks": [
                    {
                        "title": b["title"],
                        "start": b["start"].isoformat(),
                        "end":   b["end"].isoformat(),
                        "duration_mins": b["duration_mins"],
                    }
                    for b in blocks
                ],
            }
            preview = format_block_preview(blocks) + "\n\nConfirm adding these blocks? Reply: yes / cancel"
        else:
            return _reply(f"{REPLY_PREFIX}❓ Unhandled intent: {intent}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)
    except Exception as exc:
        return _reply(f"{REPLY_PREFIX}⚠️ Extraction failed: {exc}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)

    if dry_run:
        print(f"[dry-run] intent={intent}")
        print(f"[dry-run] payload={json.dumps(payload, indent=2, ensure_ascii=False)}")
        print(f"[dry-run] preview:\n{preview}")
        return ""

    # Embed message_id for reply threading
    payload["message_id"] = message_id

    # Write to queue + set pending confirm
    item_id = write_item(
        intent=intent,
        raw_message=message,
        sender=sender_id,
        channel_id=channel_id,
        source=source,
        payload=payload,
    )
    update_status(item_id, "awaiting_confirm")
    set_pending_confirm(sender_id, item_id)

    preview = f"{preview}\n`#{item_id[:8]}`"
    if source == "telegram":
        _markup = json.dumps({"inline_keyboard": [[
            {"text": "Yes ✅", "callback_data": "yes"},
            {"text": "Cancel ❌", "callback_data": "cancel"},
        ]]})
        preview = f"{preview}\n__MARKUP__:{_markup}"
    return _reply(f"{REPLY_PREFIX}{preview}", channel_id=channel_id, sender=sender_id, message_id=message_id, dry_run=dry_run, source=source)


def route(raw_input: str, dry_run: bool = False) -> str:
    """Safety-net wrapper: catches any unhandled exception from _route_impl,
    logs the full traceback to route_debug.log, and returns a readable error
    string so OpenClaw never shows its own generic 'Something went wrong' message.
    """
    import traceback as _tb
    try:
        return _route_impl(raw_input, dry_run)
    except Exception as exc:
        tb_str = _tb.format_exc()
        try:
            import datetime as _edt
            _err_path = Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "logs" / "route_debug.log"
            with open(_err_path, "a") as _ef:
                _ef.write(f"\n=== {_edt.datetime.utcnow().isoformat()} UNHANDLED in route() ===\n{tb_str}\n")
        except Exception:
            pass
        return f"{REPLY_PREFIX}⚠️ {type(exc).__name__}: {exc}"


# ── HTTP serve mode ───────────────────────────────────────────────────────────

def serve(host: str = "0.0.0.0", port: int = 18789):
    """Run a minimal HTTP server that accepts POST /route with a JSON body."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._respond(200, {"status": "ok"})
            else:
                self._respond(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/route":
                self._respond(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._respond(400, {"error": "invalid JSON"})
                return
            message = data.get("message", "")
            if not message:
                self._respond(400, {"error": "missing 'message' field"})
                return
            reply = route(message)
            self._respond(200, {"reply": reply})

        def _respond(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer((host, port), Handler)
    print(f"Sensor listening on {host}:{port}", flush=True)
    server.serve_forever()


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Aaka Away Router")
    parser.add_argument("message", nargs="?", default="", help="Message text")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run extraction without writing to queue")
    parser.add_argument("--title-only", action="store_true",
                        help="Zero-token only: print carrier/attendee/title without LLM")
    parser.add_argument("--serve", action="store_true",
                        help="Start HTTP server on port 18789")
    parser.add_argument("--port", type=int, default=18789,
                        help="Port for --serve mode (default: 18789)")
    args = parser.parse_args()

    if args.serve:
        serve(port=args.port)
        sys.exit(0)

    text = args.message or (sys.stdin.read() if not sys.stdin.isatty() else "")

    if args.title_only:
        from skills.calendar.prepare_event import (
            _detect_carrier, _detect_attendee, _detect_type, _extract_summary
        )
        from skills.calendar.add_event import build_title
        text_clean = re.sub(r'#\w+\s*', '', re.sub(r'^/(cal|add)\s*', '', text, flags=re.I)).strip()
        carrier  = _detect_carrier(text_clean)
        attendee = _detect_attendee(text_clean, skip=carrier or "") or aaka_config.group_name()
        type_str = _detect_type(text_clean)
        summary  = _extract_summary(text_clean, attendee, carrier or "", type_str)
        title    = build_title(attendee, type_str, summary, carrier_override=carrier or "")
        print(f"carrier:  {carrier!r}")
        print(f"attendee: {attendee!r}")
        print(f"type:     {type_str!r}")
        print(f"summary:  {summary!r}")
        print(f"title:    {title}")
        sys.exit(0)
    if not text:
        parser.print_help()
        sys.exit(1)

    result = route(text, dry_run=args.dry_run)
    if result:
        print(result)
