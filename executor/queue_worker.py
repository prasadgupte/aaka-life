#!/usr/bin/env python3
"""
Aaka — Home / Executor Queue Worker

Polls butler.db for confirmed items, executes side-effects (Google Calendar),
sends confirmation messages via message_send.py, then marks items
done (or error). Designed to run on the local Mac (Home) where Google OAuth tokens live.

Usage:
  python3 executor/queue_worker.py            # process all confirmed items, then exit
  python3 executor/queue_worker.py --once     # same (explicit alias)
  QUEUE_DB=./queue/dev.db python3 executor/queue_worker.py --once

Called by sync_db.sh (or launchd) every 60 s in production.
"""

import json
import os
import sys
import traceback
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent
)
sys.path.insert(0, str(BASE))

from aaka_queue.queue import (
    read_pending,
    update_status,
    increment_retry,
    reset_stuck_executing,
    write_outbox,
)

from aaka_config import reply_prefix
REPLY_PREFIX = reply_prefix()

MAX_RETRY = 3

# ── Skill Loader ──────────────────────────────────────────────────────────────
# USE_SKILL_LOADER is always on. The SkillLoader enforces requires_confirmation,
# enabled/disabled flags, and unknown-user gates — these must never be bypassed.

REGISTRY_PATH = BASE / "skills" / "registry.yaml"

_loader = None

def _get_loader():
    global _loader
    if _loader is None:
        import aaka_config
        from executor.skill_loader import SkillLoader
        _loader = SkillLoader(REGISTRY_PATH, aaka_config.LOGS_DIR)
    return _loader


# ── Dispatch helpers ──────────────────────────────────────────────────────────

def _exec_add_event(payload: dict) -> dict:
    """Write events to Google Calendar via add_event.write_event()."""
    from skills.calendar.add_event import write_event
    return write_event(payload)


def _exec_add_event_batch(payload: dict) -> dict:
    """Write multiple distinct events to Google Calendar."""
    from skills.calendar.add_event import write_event
    events = payload.get("events", [])
    total_ids: list[str] = []
    for ev in events:
        result = write_event(ev)
        total_ids.extend(result.get("event_ids", []))
    return {"event_ids": total_ids, "count": len(total_ids)}


def _exec_add_task(payload: dict) -> dict:
    """Add a task to local JSON store (migration fallback for queued items)."""
    from skills.tasks.local_tasks import add_task as _add_task
    payload.setdefault("agent", "aaka")
    task = _add_task(payload)
    return {"task_id": task["id"], "title": task["title"], "due_date": task.get("due_date", "")}


def _exec_complete_task(payload: dict) -> dict:
    """Complete a task in local JSON store (migration fallback for queued items)."""
    from skills.tasks.local_tasks import complete_task_by_id, find_by_title
    task_id = payload.get("task_id", "")
    title_hint = payload.get("title_hint", "")
    if not task_id and title_hint:
        t = find_by_title(title_hint)
        if t:
            task_id = t["id"]
            title_hint = t["title"]
    if not task_id:
        raise ValueError(f"Could not find task: {title_hint!r}")
    t = complete_task_by_id(task_id)
    return {"task_id": task_id, "title": t.get("title", title_hint)}


def _exec_queue_test(payload: dict) -> dict:
    """Minimal echo handler for /test add-queue."""
    from datetime import datetime, timezone
    processed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "status": "ok",
        "echo": "queue test executed",
        "queued_at": payload.get("enqueued_at", ""),
        "processed_at": processed_at,
        "message_id": payload.get("message_id"),
    }


def _exec_executor_echo(payload: dict) -> dict:
    """End-to-end echo test: writes reply to outbox for sensor to flush."""
    from datetime import datetime, timezone
    processed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = (
        f"🌤️ Executor echo\n"
        f"• Message: {payload.get('echo_text', '')}\n"
        f"• Enqueued: {payload.get('enqueued_at', '')}\n"
        f"• Processed: {processed_at}"
    )
    write_outbox(
        channel_id=payload.get("channel_id", payload.get("sender", "")),
        sender=payload.get("sender", ""),
        text=text,
        reply_to_message_id=payload.get("message_id"),
        source=payload.get("source", "telegram"),
    )
    return {"processed_at": processed_at}


def _exec_file_sync(payload: dict) -> dict:
    """Copy a sensor-side file to the vault."""
    import shutil
    import aaka_config
    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/Users/Shared/aaka-repo-config"))
    src = config_dir / payload["source_path"]
    namespace = payload.get("namespace") or aaka_config.default_actor()
    vault = aaka_config.vault_path_for(namespace)
    dest = vault / payload["dest_vault_path"]
    if not src.exists():
        raise FileNotFoundError(f"Source not synced yet: {payload['source_path']}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))
    return {"status": "ok", "synced": payload["dest_vault_path"]}


def _exec_file_upsert(payload: dict) -> dict:
    """Append new note lines to a vault file (create if missing). Never overwrites.

    For context files (_context.md) — routed topics that share a folder with
    file drops — entries are appended chronologically at the end of the file
    with type:context frontmatter. For legacy 04-Notes files, old behavior is
    preserved (type:log frontmatter, plain append).
    """
    import aaka_config

    topic = payload["topic"]
    lines = payload["lines"]  # pre-formatted strings, e.g. ["- 2026-05-09 text"]
    namespace = payload.get("namespace") or aaka_config.default_actor()
    vault = aaka_config.vault_path_for(namespace)
    dest_vault_path = payload["dest_vault_path"]
    dest = vault / dest_vault_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    is_new = not dest.exists()
    is_context = dest_vault_path.endswith("_context.md")
    file_type = "context" if is_context else "log"
    with open(dest, "a") as f:
        if is_new:
            f.write(f"---\ntags: [{topic}]\nowner: {namespace}\ntype: {file_type}\n---\n\n")
        for line in lines:
            f.write(line + "\n")
    return {"synced_through": payload.get("entry_count_after", 0)}


def _exec_drop_note(payload: dict) -> dict:
    """Create a markdown note in vault/00-Inbox/."""
    from skills.drop.drop_note import execute
    return execute(payload)


def _exec_drop_file(payload: dict) -> dict:
    """Move staged file to vault via tag-based routing (or inbox fallback)."""
    from skills.drop.drop_file import execute
    return execute(payload)


def _exec_keep_original(payload: dict) -> dict:
    """Restore the uncompressed original for a previously-compressed drop.

    Reads the keep_index entry written by skills/drop/drop_file._record_keep,
    moves the recycled original back into the vault next to the compressed
    file, and removes the compressed copy. Idempotent-ish: a missing index
    entry or missing recycled file returns an error result so the user gets
    a clear message instead of a silent no-op.
    """
    from skills.drop.drop_file import read_keep_index, write_keep_index
    import aaka_config

    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/Users/Shared/aaka-repo-config"))
    keep_hash = (payload.get("keep_hash") or "").lower()[:8]
    if not keep_hash:
        raise ValueError("keep_original payload missing keep_hash")

    idx = read_keep_index(config_dir)
    entry = idx.get(keep_hash)
    if not entry:
        return {"status": "missing", "keep_hash": keep_hash,
                "error_hint": f"No compressed-original record for #{keep_hash} (expired or never compressed)"}

    recycled_rel = entry.get("recycled_path", "")
    recycled = config_dir / recycled_rel
    if not recycled.exists():
        return {"status": "missing", "keep_hash": keep_hash,
                "error_hint": f"Original file gone from recycle_bin: {recycled_rel}"}

    owner = entry.get("vault_owner", "")
    if owner == "shared":
        vault_root = aaka_config.SHARED_VAULT_PATH
    else:
        vault_root = aaka_config.vault_path_for(owner)
    if vault_root is None:
        return {"status": "missing", "keep_hash": keep_hash,
                "error_hint": f"Vault root for '{owner}' not configured"}

    compressed_path = vault_root / entry.get("vault_dest", "")
    dest_dir = compressed_path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Use the original filename — drop the _c suffix the compressed copy got.
    original_name = entry.get("original_filename") or recycled.name
    restored = dest_dir / original_name
    if restored.exists():
        stem, ext = os.path.splitext(original_name)
        for i in range(2, 1000):
            candidate = dest_dir / f"{stem}-{i}{ext}"
            if not candidate.exists():
                restored = candidate
                break

    shutil.move(str(recycled), str(restored))

    # Remove the compressed copy
    removed_compressed = False
    if compressed_path.exists() and compressed_path.resolve() != restored.resolve():
        try:
            compressed_path.unlink()
            removed_compressed = True
        except OSError:
            pass

    # Drop the index entry — original is no longer recyclable
    idx.pop(keep_hash, None)
    write_keep_index(config_dir, idx)

    return {
        "status": "ok",
        "keep_hash": keep_hash,
        "file": str(restored.relative_to(vault_root)),
        "original_filename": original_name,
        "removed_compressed": removed_compressed,
    }


def _exec_undo_drop(payload: dict) -> dict:
    """Move a previously filed document back to 00-Inbox/{namespace}/."""
    import json as _json
    import sqlite3 as _sq

    original_item_id = payload["original_item_id"]
    namespace = payload.get("namespace", "user")

    _db_path = os.environ.get("QUEUE_DB") or str(
        Path(os.environ.get("AAKA_CONFIG_DIR", "/Users/Shared/aaka-repo-config"))
        / "data" / "queue" / "butler.db"
    )
    with _sq.connect(_db_path) as conn:
        conn.row_factory = _sq.Row
        row = conn.execute(
            "SELECT * FROM queue_items WHERE id=? AND intent='drop_file' LIMIT 1",
            (original_item_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"No drop_file item with id {original_item_id}")

    result_data = _json.loads(row["result"] or "{}") if isinstance(row["result"], str) else (row["result"] or {})
    filed_rel = result_data.get("file", "")
    if not filed_rel:
        raise ValueError("Original item has no filed path")

    vault = aaka_config.vault_path_for(namespace)
    filed_path = vault / filed_rel
    if not filed_path.exists():
        raise FileNotFoundError(f"File not found: {filed_rel}")

    inbox = vault / "00-Inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    new_path = inbox / filed_path.name
    shutil.move(str(filed_path), str(new_path))

    # Move companion .meta.md if present
    meta_old = Path(str(filed_path) + ".meta.md")
    if meta_old.exists():
        shutil.move(str(meta_old), str(inbox / meta_old.name))

    rel_path = str(new_path.relative_to(vault))
    return {
        "status": "ok",
        "file": rel_path,
        "original_filename": result_data.get("original_filename", filed_path.name),
        "message_id": payload.get("message_id"),
    }


def _exec_route_tags(payload: dict) -> dict:
    """Resolve tags against vault/System/references.yaml — zero LLM calls."""
    from tools.inbox_router import route as inbox_route
    import aaka_config

    tags = payload.get("tags", [])
    carrier_members = [m for m in aaka_config.members() if m.get("is_carrier")]
    namespace = payload.get("namespace") or (carrier_members[0]["id"] if carrier_members else "user")
    result = inbox_route(tags, namespace=namespace, vault=aaka_config.vault_path_for(namespace))
    return {
        "status": "ok",
        "entities": list(result.get("entities", {}).keys()),
        "actions":  list(result.get("actions", {}).keys()),
        "unknown":  result.get("unknown", []),
        "outcomes": result.get("outcomes", []),
        "message_id": payload.get("message_id"),
    }


def _exec_plan_slots(payload: dict) -> dict:
    """Find free slots via Google Calendar and write reply to outbox."""
    import re
    from skills.calendar.plan import parse_plan_args, find_plan_slots, format_plan_reply

    raw = payload.get("raw_message", "")
    args_text = re.sub(r'^/plan\s*', '', raw, flags=re.I).strip()
    args = parse_plan_args(args_text)
    available, reserved = find_plan_slots(
        dates=args["dates"],
        duration_minutes=args["duration_minutes"],
        time_window=args["time_window"],
        use_work_hours=args["use_work_hours"],
        ignore_reserved=args["ignore_reserved"],
        tags=args.get("tags"),
    )
    reply_text = REPLY_PREFIX + format_plan_reply(
        dates=args["dates"],
        available=available,
        reserved=reserved,
        duration_minutes=args["duration_minutes"],
        recap=args.get("recap", ""),
    )
    write_outbox(
        channel_id=payload.get("channel_id", payload.get("sender", "")),
        sender=payload.get("sender", ""),
        text=reply_text,
        reply_to_message_id=payload.get("message_id"),
        source=payload.get("source", "telegram"),
    )
    return {"status": "ok", "message_id": payload.get("message_id")}


def _exec_day_schedule(payload: dict) -> dict:
    """Fetch all events for a given day and write reply to outbox."""
    import re
    from skills.calendar.plan import parse_day_date
    from skills.calendar.availability import fetch_day_events
    import aaka_config as _ac

    raw = payload.get("raw_message", "")
    args_text = re.sub(r'^/day\s*', '', raw, flags=re.I).strip()
    target_date = parse_day_date(args_text)

    cal_ids = _ac.all_calendar_ids()
    events = fetch_day_events(cal_ids, target_date.isoformat())

    date_label = target_date.strftime("%a %-d %b")
    if not events:
        text = f"{REPLY_PREFIX}📅 {date_label} — nothing on the calendar."
    else:
        lines = [f"{REPLY_PREFIX}📅 {date_label}"]
        for ev in events:
            if ev["all_day"]:
                lines.append(f"  • {ev['title']}")
            else:
                lines.append(f"  • {ev['start']}–{ev['end']}  {ev['title']}")
        text = "\n".join(lines)

    write_outbox(
        channel_id=payload.get("channel_id", payload.get("sender", "")),
        sender=payload.get("sender", ""),
        text=text,
        reply_to_message_id=payload.get("message_id"),
        source=payload.get("source", "telegram"),
    )
    return {"status": "ok", "message_id": payload.get("message_id")}


def _exec_fix_event(payload: dict) -> dict:
    """Update a calendar event to assign a carrier."""
    import re as _re
    from skills.calendar.gog import update_event

    event_id = payload["event_id"]
    calendar_id = payload.get("calendar_id", "")
    old_title = payload.get("title", "")
    carrier_name = payload["carrier_name"]
    carrier_email = payload.get("carrier_email", "")

    # Replace carrier placeholder in title
    if '🤝' in old_title and '❓' in old_title:
        # Standard format: "🤝 ❓" → "🤝 CarrierName"
        new_title = _re.sub(r'🤝\s*❓', f'🤝 {carrier_name}', old_title)
    else:
        # No carrier marker in title — insert "(🤝 CarrierName)" after attendee name
        import aaka_config as _ac
        attendee = payload.get("attendee", "")
        if not attendee:
            # Try to detect attendee from title
            for name in _ac.needs_carrier():
                if _re.search(rf'\b{_re.escape(name)}\b', old_title, _re.I):
                    attendee = name
                    break
        if attendee and _re.search(rf'\b{_re.escape(attendee)}\b', old_title, _re.I):
            new_title = _re.sub(
                rf'(\b{_re.escape(attendee)}\b)',
                rf'\1 (🤝 {carrier_name})',
                old_title, count=1, flags=_re.I,
            )
        else:
            new_title = f"{old_title} (🤝 {carrier_name})"

    add_guests = [carrier_email] if carrier_email else []

    result = update_event(
        event_id=event_id,
        calendar_id=calendar_id,
        title=new_title,
        add_guests=add_guests,
    )

    return {
        "event_id": event_id,
        "old_title": old_title,
        "new_title": new_title,
        "title": new_title,
        "carrier": carrier_name,
        "count": 1,
    }


def _exec_gmail_label(payload: dict) -> dict:
    """Process an ingested Gmail email via label-specific LLM skill.

    Supports both single-action (legacy) and multi-action configs:

    Legacy (single action):
      label_cfg:  prompt/prompt_file + output + optional topic

    Multi-action:
      label_cfg:  actions:
                    - prompt/prompt_file + output + optional topic
                    - ...

    Each action gets its own LLM call and is routed independently.
    """
    import aaka_config as _cfg
    label_name = payload.get("label", "")
    label_cfg = next(
        (l for l in _cfg.gmail_labels() if l["label"] == label_name),
        None,
    )
    if not label_cfg:
        raise ValueError(f"No gmail_labels config for label {label_name!r}")

    from llm import call_llm

    # Normalise to a list of actions (backward-compat: single action at top level)
    if "actions" in label_cfg:
        actions = label_cfg["actions"]
    else:
        actions = [label_cfg]

    results = []
    for action in actions:
        # Resolve prompt: inline 'prompt' OR external 'prompt_file'
        prompt_text = action.get("prompt", "")
        prompt_file = action.get("prompt_file", "")
        if prompt_file:
            prompt_path = Path(os.path.expanduser(prompt_file))
            if prompt_path.exists():
                prompt_text = prompt_path.read_text().strip()
            else:
                raise FileNotFoundError(f"prompt_file not found: {prompt_path}")
        if not prompt_text:
            raise ValueError(f"Action in label {label_name!r} has no 'prompt' or 'prompt_file'")

        prompt = (
            f"{prompt_text}\n\n"
            f"Email:\n"
            f"From: {payload.get('from_addr', '')}\n"
            f"Subject: {payload.get('subject', '')}\n"
            f"Date: {payload.get('date', '')}\n\n"
            f"{payload.get('body', '')}"
        )
        text = call_llm(prompt).strip()
        output = action.get("output", "telegram")

        if output == "note":
            from skills.notes.note_writer import append_note
            topic = action.get("topic", "inbox")
            append_note(topic, text, namespace="user")
            results.append({"output": "note", "topic": topic, "preview": text[:120]})

        elif output == "task":
            from skills.tasks.local_tasks import add_task as _add_task
            _add_task({"title": text[:80], "description": text, "tags": ["gmail"]})
            results.append({"output": "task", "title": text[:80]})

        else:  # telegram
            group_id = os.environ.get("TELEGRAM_GROUP_ID", "")
            if group_id:
                write_outbox(
                    channel_id=group_id,
                    sender="gmail_label",
                    text=f"📧 *{label_name}* — {text}",
                    source="telegram",
                    silent=True,
                )
            results.append({"output": "telegram", "preview": text[:120]})

    # Check if this label has a tag route with gmail_label field → auto-file attachments
    message_id = payload.get("message_id", "")
    member_id  = payload.get("member_id", "")
    if message_id:
        try:
            from tools.inbox_router import tag_for_gmail_label
            tag_info = tag_for_gmail_label(label_name)
            if tag_info:
                from aaka_queue.queue import write_item, update_status as _upd
                att_item_id = write_item(
                    intent="gmail_attachment_file",
                    raw_message=f"gmail_attachment:{label_name}",
                    sender="gmail_label",
                    channel_id=payload.get("_queue_id", ""),
                    source="telegram",
                    payload={
                        "message_id": message_id,
                        "member_id":  member_id,
                        "label":      label_name,
                        "tag_name":   tag_info["tag_name"],
                        "dest_path":  tag_info["path"],
                        "dest_owner": tag_info["owner"],
                    },
                )
                _upd(att_item_id, "confirmed")
                print(f"[worker] gmail_label: queued gmail_attachment_file [{att_item_id[:8]}] "
                      f"for label={label_name!r} tag={tag_info['tag_name']!r}")
        except Exception as _att_exc:
            print(f"[warn] gmail_label: attachment filer queue failed: {_att_exc}", file=sys.stderr)

    return {"actions_run": len(results), "results": results}


def _exec_bday_wish(payload: dict) -> dict:
    """Send birthday wishes to a contact via WhatsApp (preferred) or email."""
    from skills.outbox.send_contact import send_to_contact
    phone = payload.get("phone", "") or payload.get("mobile", "")
    email = payload.get("email", "")
    channel = "whatsapp" if phone else "email"
    addr = phone if phone else email
    if not addr:
        return {"sent": False, "error": "No phone or email in payload",
                "first_name": payload.get("first_name", ""), "name": payload.get("name", "")}
    result = send_to_contact(
        name=payload["name"],
        first_name=payload["first_name"],
        phone=phone if channel == "whatsapp" else "",
        email=email if channel == "email" else "",
        message=payload["message"],
        from_member_id=payload.get("from_member_id", ""),
        channel=channel,
    )
    return {**result, "first_name": payload["first_name"], "name": payload["name"],
            "channel": channel}


def _send_approval_request(item: dict) -> None:
    """Send approval request with inline keyboard for executor-side awaiting_confirm items.

    Called when check_confirmation() raises AwaitingConfirmation for items like
    agent_job and bday_wish that are created programmatically (not from a user
    message) and thus have no sensor-side approval message.
    """
    from aaka_queue.queue import set_pending_confirm
    try:
        item_id   = item["id"]
        sender    = item.get("sender", "")
        channel   = item.get("channel_id", sender)
        intent    = item.get("intent", "?")
        source    = item.get("source", "telegram")
        item_payload = json.loads(item["payload"]) if item.get("payload") else {}

        # Build a short human preview per intent
        if intent == "agent_job":
            agent_id  = item_payload.get("agent_id", "?")
            job_name  = item_payload.get("job_name", "?")
            preview = f"🤖 *Agent job approval*\n\nAgent: `{agent_id}`\nJob: `{job_name}`"
        elif intent == "bday_wish":
            name = item_payload.get("name", item_payload.get("contact_name", "?"))
            channel_label = item_payload.get("channel", "?")
            preview = f"🎂 *Birthday auto-send*\n\nContact: {name}\nChannel: {channel_label}"
        else:
            preview = f"🔔 *Approval required ({intent})*"

        preview = f"{preview}\n\nApprove?\n`#{item_id[:8]}`"
        markup = {"inline_keyboard": [[
            {"text": "Yes ✅", "callback_data": "yes"},
            {"text": "Cancel ❌", "callback_data": "cancel"},
        ]]}
        write_outbox(channel_id=channel, sender=sender, text=preview,
                     source=source, reply_markup=markup)
        set_pending_confirm(sender, item_id)
        print(f"[worker] {item_id[:8]}  approval request sent for {intent}")
    except Exception:
        print(f"[warn] _send_approval_request failed: {traceback.format_exc()}", file=sys.stderr)


def _exec_confirm_approval(payload: dict) -> dict:
    """Re-send the approval preview + buttons for an awaiting_confirm queue item."""
    import re as _re
    import sqlite3 as _sq
    from aaka_queue.queue import write_outbox, set_pending_confirm, QUEUE_DB

    prefix = payload.get("hash", "")
    sender = payload.get("sender", "")
    channel_id = payload.get("channel_id", sender)

    with _sq.connect(str(QUEUE_DB), timeout=5) as conn:
        conn.row_factory = _sq.Row
        row = conn.execute(
            "SELECT * FROM queue_items WHERE id LIKE ? AND status='awaiting_confirm' "
            "ORDER BY created_at DESC LIMIT 1",
            (f"{prefix}%",),
        ).fetchone()

    if not row:
        return {"text": f"⚠️ No awaiting_confirm item matching #{prefix}"}

    item = dict(row)
    item_id = item["id"]
    item_payload = json.loads(item["payload"]) if item.get("payload") else {}
    intent = item["intent"]

    if intent == "linkedin_post":
        post_text = item_payload.get("text", "")
        tg_text = _re.sub(r'\*\*(.+?)\*\*', r'*\1*', post_text)
        sched = item_payload.get("schedule_at") or item.get("schedule_at")
        sched_str = f"\n⏰ Scheduled: {sched[:16].replace('T', ' ')} UTC" if sched else ""
        header = "📤 *LinkedIn post:*\n\n"
        footer = f"{sched_str}\n\nApprove?\n`#{item_id[:8]}`"
        max_body = 3800 - len(header) - len(footer)
        if len(tg_text) > max_body:
            tg_text = tg_text[:max_body - 1] + "…"
        preview = f"{header}{tg_text}{footer}"
    else:
        preview = f"🔔 *Approval required ({intent})*\n\nApprove?\n`#{item_id[:8]}`"

    markup = {"inline_keyboard": [[
        {"text": "Post ✅", "callback_data": "yes"},
        {"text": "Cancel ❌", "callback_data": "cancel"},
    ]]}
    write_outbox(channel_id=channel_id, sender=sender, text=preview, source="telegram", reply_markup=markup)
    set_pending_confirm(sender, item_id)
    return {"text": f"✅ Approval buttons re-sent for #{item_id[:8]}"}


def _exec_linkedin_post(payload: dict) -> dict:
    """Post to LinkedIn.

    If schedule_at is set, creates a DRAFT in LinkedIn so the user can review,
    edit, and schedule/publish it from LinkedIn Creator Studio.
    If schedule_at is absent, publishes immediately.
    """
    from skills.linkedin.post import post as li_post
    text = payload.get("text", "")
    image = payload.get("image_path")
    schedule_at = payload.get("schedule_at")
    if not text:
        raise ValueError("No post text in payload")
    result = li_post(text, image_path=image, schedule_at=schedule_at)
    result["draft"] = bool(schedule_at)
    return result


def _exec_agent_job(payload: dict) -> dict:
    """Dispatch an agent's scheduled job.

    If payload contains ``claude_prompt`` and ``project_path``, runs the Claude
    CLI in non-interactive mode (``-p``) with the project directory as cwd.

    --dangerously-skip-permissions is only passed when the agent has
    ``allow_dangerous: true`` in its permissions column. Default: off.
    """
    agent_id = payload.get("agent_id", "")
    job_name = payload.get("job_name", "")
    print(f"[worker] agent_job fired: agent={agent_id} job={job_name}")

    claude_prompt = payload.get("claude_prompt")
    project_path = payload.get("project_path")
    if claude_prompt and project_path:
        import subprocess
        from aaka_queue.queue import _connect
        # Check agent permissions
        allow_dangerous = False
        try:
            conn = _connect()
            row = conn.execute(
                "SELECT permissions FROM agent_registry WHERE id=?", (agent_id,)
            ).fetchone()
            if row and row["permissions"]:
                import json as _json
                perms = _json.loads(row["permissions"] or "{}")
                allow_dangerous = bool(perms.get("allow_dangerous", False))
        except Exception as _perm_exc:
            print(f"[worker] perm check failed for {agent_id}: {_perm_exc}")

        cmd = ["claude", "-p", claude_prompt, "--no-session-persistence"]
        if allow_dangerous:
            cmd.append("--dangerously-skip-permissions")
            print(f"[worker] agent {agent_id} has allow_dangerous=true")

        try:
            result = subprocess.run(
                cmd, cwd=project_path,
                capture_output=True, text=True, timeout=300,
            )
            print(f"[worker] claude exited {result.returncode} for {agent_id}/{job_name}")
            return {
                "agent_id": agent_id, "job_name": job_name,
                "status": "executed", "exit_code": result.returncode,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-500:],
            }
        except subprocess.TimeoutExpired:
            print(f"[worker] claude timed out for {agent_id}/{job_name}")
            return {"agent_id": agent_id, "job_name": job_name, "status": "timeout"}

    return {"agent_id": agent_id, "job_name": job_name, "status": "fired"}


def _exec_mail_fetch(payload: dict) -> dict:
    from skills.mail.fetch import execute
    return execute(payload)


def _exec_mail_view(payload: dict) -> dict:
    import re as _re
    from skills.mail.viewer import list_accounts, list_messages, read_message
    message = payload.get("message", "")
    args = _re.sub(r"^/mail\s*|^m\s*", "", message, flags=_re.I).strip().split()
    if not args:
        return {"text": list_accounts()}
    if args[0].lower() == "read" and len(args) >= 3:
        account_name = args[1]
        try:
            n = int(args[2])
        except ValueError:
            n = 1
        return {"text": read_message(account_name, n)}
    account_name = args[0]
    n = 10
    if len(args) >= 2:
        try:
            n = int(args[1])
        except ValueError:
            pass
    return {"text": list_messages(account_name, n)}


def _exec_pw_manage(payload: dict) -> dict:
    import re as _re
    from tools.password_store import PasswordStore
    message = payload.get("message", "")
    args = _re.sub(r"^/pw\s*|^/pass\s*", "", message, flags=_re.I).strip().split(None, 1)
    sub = args[0].lower() if args else "help"
    rest = args[1] if len(args) > 1 else ""
    store = PasswordStore()
    if sub == "count":
        return {"text": f"🔑 Password store: {store.count()} entries"}
    if sub == "list":
        group = rest.strip() or None
        entries = store.list_entries(group=group)
        if not entries:
            return {"text": "🔑 No entries found."}
        lines = [f"🔑 *Passwords* ({len(entries)})\n"]
        for e in entries[:50]:
            line = f"• {e['name']}"
            if e.get("username"):
                line += f"  ({e['username']})"
            if e.get("grouping"):
                line += f"  [{e['grouping']}]"
            lines.append(line)
        if len(entries) > 50:
            lines.append(f"… and {len(entries) - 50} more. Use /pw search to filter.")
        return {"text": "\n".join(lines)}
    if sub == "search":
        if not rest:
            return {"text": "Usage: /pw search <query>"}
        results = store.search(rest)
        if not results:
            return {"text": f"🔑 No matches for {rest!r}"}
        lines = [f"🔑 *Search: {rest}* ({len(results)} matches)\n"]
        for e in results[:30]:
            line = f"• {e['name']}"
            if e.get("username"):
                line += f"  ({e['username']})"
            if e.get("url"):
                line += f"  — {e['url']}"
            lines.append(line)
        return {"text": "\n".join(lines)}
    if sub in ("add", "get", "import", "delete", "export", "update"):
        return {"text": "🔒 Password write operations are only available via local CLI.\n\npython3 tools/pw_setup.py\npython3 tools/pw_import.py export.csv"}
    return {"text": (
        "🔑 *Password store*\n"
        "/pw list [group] — list entries\n"
        "/pw search <query> — search by name/url\n"
        "/pw count — total entries\n\n"
        "Write operations (local CLI only):\n"
        "  python3 tools/pw_setup.py — initial setup\n"
        "  python3 tools/pw_import.py export.csv — import LastPass"
    )}


def _exec_gmail_attachment_file(payload: dict) -> dict:
    """File Gmail attachments by delegating to skills/mail/gmail_attachment_filer.py."""
    from skills.mail.gmail_attachment_filer import execute as _file_attachments
    return _file_attachments(payload)


_DISPATCHERS = {
    "add_event":       _exec_add_event,
    "add_event_batch": _exec_add_event_batch,
    "plan_slots":     _exec_plan_slots,
    "day_schedule":   _exec_day_schedule,
    "add_task":       _exec_add_task,
    "complete_task":  _exec_complete_task,
    "queue_test":     _exec_queue_test,
    "executor_echo":  _exec_executor_echo,
    "route_tags":     _exec_route_tags,
    "drop_note":      _exec_drop_note,
    "drop_file":      _exec_drop_file,
    "undo_drop":      _exec_undo_drop,
    "keep_original":  _exec_keep_original,
    "file_sync":      _exec_file_sync,
    "file_upsert":    _exec_file_upsert,
    "fix_event":      _exec_fix_event,
    "bday_wish":      _exec_bday_wish,
    "gmail_label":           _exec_gmail_label,
    "gmail_attachment_file": lambda p: _exec_gmail_attachment_file(p),
    "agent_job":             _exec_agent_job,
    "confirm_approval": _exec_confirm_approval,
    "linkedin_post":  _exec_linkedin_post,
    "mail_fetch":     _exec_mail_fetch,
    "mail_view":      _exec_mail_view,
    "pw_manage":      _exec_pw_manage,
}


def _voice_ctx_from_result(item: dict, result: dict) -> dict:
    """Build VoiceContext dict from a queue item and its execution result."""
    import aaka_config
    member = aaka_config.member_by_sender(item.get("sender", ""))
    payload_raw = item.get("payload", "{}")
    try:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
    except Exception:
        payload = {}
    return {
        "name": member["name"] if member else "",
        "count": result.get("count", result.get("legs_logged", 1)),
        "item": result.get("title", payload.get("title", "")),
        "carrier": payload.get("carrier", ""),
    }


def _send_confirmation(item: dict, result: dict) -> None:
    """Queue a success message to outbox for sensor to flush."""
    try:
        intent   = item["intent"]
        sender   = item["sender"]
        channel  = item.get("channel_id", sender)
        reply_to = result.get("message_id")

        from skills.voice import get_voice
        if intent == "add_event_batch":
            n = result.get("count", 0)
            vctx = _voice_ctx_from_result(item, result)
            vctx["count"] = n
            text = f"{REPLY_PREFIX}{get_voice().apply('add_event', '', vctx)}"
        elif intent == "add_event":
            n = result.get("count", 1)
            payload_raw = item.get("payload", "{}")
            try:
                payload_data = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
                occs = payload_data.get("occurrences", [])
            except Exception:
                payload_data = {}
                occs = []
            if n > 5 and occs:
                import datetime as _dt
                word = f"{n} events"
                first5 = occs[:5]
                lines = [f"{REPLY_PREFIX}✅ Added {word} to the calendar.", "📅 First 5:"]
                for occ in first5:
                    date = occ.get("date", "")
                    st   = occ.get("start_time", "")
                    et   = occ.get("end_time", "")
                    try:
                        d = _dt.date.fromisoformat(date)
                        date_str = d.strftime("%a %-d %b")
                    except Exception:
                        date_str = date
                    time_str = f"  {st}–{et}" if st and et else ""
                    title = payload_data.get("title", "")
                    lines.append(f"  {date_str}{time_str}  {title}")
                lines.append(f"(+{n - 5} more — check Google Calendar for the full list)")
                text = "\n".join(lines)
            else:
                vctx = _voice_ctx_from_result(item, result)
                vctx["count"] = n
                vctx["item"] = payload_data.get("title", vctx.get("item", ""))
                text = f"{REPLY_PREFIX}{get_voice().apply('add_event', '', vctx)}"
        elif intent == "add_task":
            title = result.get("title", "")
            due = result.get("due_date", "")
            vctx = _voice_ctx_from_result(item, result)
            vctx["item"] = title
            voiced = get_voice().apply("add_task", "", vctx)
            due_str = f"\n📅 {due}" if due else ""
            text = f"{REPLY_PREFIX}{voiced}{due_str}"
        elif intent == "complete_task":
            vctx = _voice_ctx_from_result(item, result)
            text = f"{REPLY_PREFIX}{get_voice().apply('complete_task', '', vctx)}"
        elif intent == "queue_test":
            text = f"{REPLY_PREFIX}✅ Queue test executed successfully."
        elif intent in ("file_sync", "file_upsert"):
            # silent — background sync, no user notification
            return
        elif intent in ("plan_slots", "day_schedule"):
            # these send their reply directly; skip generic confirmation
            return
        elif intent == "executor_echo":
            # executor_echo writes its own outbox entry; skip generic confirmation
            return
        elif intent == "drop_note":
            filed = result.get("file", "inbox")
            tags = result.get("tags", [])
            tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
            text = f"{REPLY_PREFIX}📝 Note filed → {filed} {tag_str}"
        elif intent == "drop_file":
            from skills.drop.format import render_drop_success
            text = REPLY_PREFIX + render_drop_success(
                orig_filename=result.get("original_filename", "file"),
                filed_rel=result.get("file", "inbox"),
                renamed_to=result.get("renamed_to"),
                compressed=result.get("compressed", False),
                original_size_bytes=result.get("original_size_bytes"),
                new_size_bytes=result.get("new_size_bytes"),
                keep_hash=result.get("keep_hash", ""),
                unknown_tags=result.get("unknown_tags") or [],
                routed=result.get("routed", True),
                error_hint=result.get("error_hint"),
                learned=result.get("learned") or [],
            )
        elif intent == "undo_drop":
            text = f"{REPLY_PREFIX}↩️ Moved back: {result.get('original_filename', 'file')} → {result.get('file', 'inbox')}"
        elif intent == "keep_original":
            from skills.drop.format import render_keep_result
            text = f"{REPLY_PREFIX}{render_keep_result(result)}"
        elif intent == "bday_wish":
            first = result.get("first_name", "contact")
            if result.get("sent"):
                text = f"{REPLY_PREFIX}✅ Sent birthday wishes to {first} by email!"
            else:
                err = result.get("error", "unknown error")
                text = f"{REPLY_PREFIX}⚠️ Email to {first} failed: {err}"
        elif intent == "fix_event":
            carrier = result.get("carrier", "")
            new_title = result.get("new_title", "")
            text = f"{REPLY_PREFIX}✅ {carrier} assigned as carrier.\n📅 {new_title}"
        elif intent == "route_tags":
            entities = result.get("entities", [])
            actions  = result.get("actions", [])
            outcomes = result.get("outcomes", [])
            unknown  = result.get("unknown", [])
            lines = [f"{REPLY_PREFIX}Tag routing complete — 0 LLM calls"]
            if entities: lines.append(f"Entity: {', '.join(entities)}")
            if actions:  lines.append(f"Action: {', '.join(actions)}")
            if unknown:  lines.append(f"Unknown: {', '.join(unknown)}")
            for o in outcomes:
                lines.append(f"→ skill={o.get('skill', '—')}  dest={o.get('file_to', '—')}")
            if not outcomes:
                lines.append("→ No outcomes matched")
            text = "\n".join(lines)
        elif intent == "linkedin_post":
            post_id = result.get("post_id", "")
            preview = result.get("text", "")[:80]
            if result.get("draft"):
                text = f"{REPLY_PREFIX}📝 LinkedIn draft created — [review & schedule](https://www.linkedin.com/feed/)\n_{preview}…_"
            else:
                text = f"{REPLY_PREFIX}✅ Posted to LinkedIn!\n_{preview}…_"
            if post_id and post_id != "unknown":
                text += f"\nID: `{post_id}`"
        elif intent == "mail_fetch":
            total = result.get("total_fetched", 0)
            checked = result.get("accounts_checked", 0)
            text = f"{REPLY_PREFIX}📬 Fetched {total} new message(s) from {checked} account(s)."
            results = result.get("results", [])
            for r in results:
                if r.get("fetched", 0) > 0 or r.get("error"):
                    line = f"\n• {r['name']}: {r['fetched']} new"
                    if r.get("error"):
                        line += f" ⚠️ {r['error'][:60]}"
                    text += line
        elif intent in ("mail_view", "pw_manage", "confirm_approval"):
            text = result.get("text", f"{REPLY_PREFIX}✅ Done.")
        else:
            text = f"{REPLY_PREFIX}✅ Done ({intent})."

        text = f"{text}\n`#{item['id'][:8]}`"
        # File intents are silent — no notification buzz
        _silent = intent in ("drop_file", "drop_note", "file_sync")
        write_outbox(
            channel_id=channel, sender=sender, text=text,
            reply_to_message_id=reply_to,
            source=item.get("source", "telegram"),
            silent=_silent,
        )
        # Queue a ✅ reaction on the original message (sensor-side "⏳" message)
        if intent == "fix_event":
            payload_raw = item.get("payload", "{}")
            try:
                p = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            except Exception:
                p = {}
            orig_msg_id = p.get("message_id")
            orig_channel = p.get("channel_id", channel)
            orig_source = p.get("source", item.get("source", "telegram"))
            if orig_msg_id:
                write_outbox(
                    channel_id=orig_channel, sender=sender,
                    text=f"__react:✅:{orig_msg_id}",
                    source=orig_source,
                )
    except Exception:
        # Best-effort; don't fail the job because of a send error
        print(f"[warn] confirmation outbox write failed: {traceback.format_exc()}", file=sys.stderr)


def _send_error(item: dict, error_msg: str) -> None:
    # Background intents: failure is not user-visible — don't spam the chat
    if item.get("intent") in ("file_sync", "file_upsert"):
        return
    try:
        text = f"{REPLY_PREFIX}⚠️ Could not process your request: {error_msg}\n`#{item['id'][:8]}`"
        write_outbox(
            channel_id=item.get("channel_id", item["sender"]),
            sender=item["sender"],
            text=text,
            source=item.get("source", "telegram"),
        )
    except Exception:
        pass


# ── Main loop ─────────────────────────────────────────────────────────────────

def process_all() -> int:
    """Process all confirmed queue items. Returns number processed."""
    # Crash-recovery: reset any items left in 'executing' from a previous crash
    reset_count = reset_stuck_executing()
    if reset_count:
        print(f"[worker] reset {reset_count} stuck 'executing' item(s) → 'confirmed'")

    # Promote scheduled items whose time has arrived to 'confirmed'
    from aaka_queue.queue import read_scheduled_ready
    scheduled = read_scheduled_ready()
    for s in scheduled:
        update_status(s["id"], "confirmed")
        print(f"[worker] scheduled item {s['id'][:8]} promoted to confirmed (intent={s['intent']})")

    items = read_pending()
    if not items:
        return 0

    print(f"[worker] processing {len(items)} item(s)")
    processed = 0

    for item in items:
        item_id = item["id"]
        intent  = item["intent"]
        payload = json.loads(item["payload"])
        payload.setdefault("_queue_id", item_id)

        print(f"[worker] {item_id[:8]}  intent={intent}")
        update_status(item_id, "executing")

        try:
            from executor.skill_loader import (
                SkillNotFound, SkillDisabled, SkillNotAllowed,
                UnknownUser, AwaitingConfirmation,
            )
            loader = _get_loader()
            try:
                skill = loader.resolve(intent, sender=item.get("sender", ""))
            except UnknownUser as exc:
                update_status(item_id, "awaiting_confirm", error_msg=str(exc))
                continue
            loader.validate(skill, context={"runs_on": "executor"})
            try:
                loader.check_confirmation(skill, item_id)
            except AwaitingConfirmation as exc:
                update_status(item_id, "awaiting_confirm", error_msg=str(exc))
                _send_approval_request(item)
                continue
            # Pass legacy dispatcher as fallback for skills with no entrypoint
            fallback = _DISPATCHERS.get(intent)
            result = loader.execute(skill, payload, dispatcher_fn=fallback if not skill.get("entrypoint") else None)

            update_status(item_id, "done", result=result)
            _send_confirmation(item, result)
            print(f"[worker] {item_id[:8]}  ✓ done  result={result}")
            processed += 1

        except Exception as exc:
            err = str(exc)
            # urllib.error.HTTPError tracebacks balloon to ~5KB each through the
            # request stack — log just the one-line summary so a flapping API
            # doesn't fill the log with duplicated stacks.
            import urllib.error
            if isinstance(exc, urllib.error.HTTPError):
                print(f"[worker] {item_id[:8]}  ✗ error: {err}", file=sys.stderr)
            else:
                tb = traceback.format_exc()
                print(f"[worker] {item_id[:8]}  ✗ error: {err}\n{tb}", file=sys.stderr)

            retry = increment_retry(item_id)
            if retry >= MAX_RETRY:
                update_status(item_id, "error", error_msg=f"{err} (after {retry} retries)")
                _send_error(item, err)
            else:
                # Put back to confirmed for next run
                update_status(item_id, "confirmed", error_msg=f"retry {retry}: {err}")

    # After processing queue items, run inbox worker to pick up any new vault files
    if processed > 0:
        try:
            from tools.inbox_worker import run as inbox_run
            import aaka_config as _ac
            # inbox_worker now handles per-member vaults internally
            from tools.inbox_worker import run as _inbox_run_all
            for m in _ac.members():
                mid = m.get("id")
                if mid and m.get("role") == "adult":
                    _inbox_run_all([mid], vault=_ac.vault_path_for(mid))
        except Exception:
            print(f"[worker] inbox_worker failed (non-fatal): {traceback.format_exc()}", file=sys.stderr)

    return processed


if __name__ == "__main__":
    import argparse, time, signal
    parser = argparse.ArgumentParser(description="Aaka Home Queue Worker")
    parser.add_argument("--once", action="store_true", help="Process pending items and exit (default)")
    parser.add_argument("--daemon", action="store_true", help="Run continuously, polling every --poll-interval seconds")
    parser.add_argument("--poll-interval", type=int, default=3, metavar="SECS", help="Seconds between polls in daemon mode (default: 3)")
    parser.add_argument("--sync-interval", type=int, default=15, metavar="SECS", help="Seconds between VPS sync cycles (default: 15)")
    parser.add_argument("--no-sync", action="store_true", help="Disable VPS sync (local-only development)")
    args = parser.parse_args()

    if args.daemon:
        _running = True

        def _stop(sig, frame):
            global _running
            print(f"[worker] signal {sig} received — stopping", flush=True)
            _running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        # VPS sync setup
        _sync_cfg = None
        if not args.no_sync:
            from executor.vps_sync import load_config, run_sync_cycle
            _sync_cfg = load_config()
            if _sync_cfg:
                print(f"[worker] VPS sync enabled (every {args.sync_interval}s → {_sync_cfg.vps_host})", flush=True)
            else:
                print("[worker] VPS sync disabled (VPS_HOST/VPS_DB_PATH not set)", flush=True)
        _last_sync = 0.0

        print(f"[worker] daemon started, polling every {args.poll_interval}s", flush=True)
        while _running:
            now = time.monotonic()

            # Periodic VPS sync
            if _sync_cfg and (now - _last_sync) >= args.sync_interval:
                try:
                    stats = run_sync_cycle(_sync_cfg)
                    _last_sync = time.monotonic()
                    if stats.pulled or stats.pushed or stats.outbox:
                        print(f"[sync] pulled={stats.pulled} pushed={stats.pushed} "
                              f"outbox={stats.outbox} ({stats.duration_ms}ms)", flush=True)
                except Exception:
                    print(f"[sync] error:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                    _last_sync = time.monotonic()

            # Process queue items
            try:
                n = process_all()
                if n:
                    print(f"[worker] processed {n} item(s)", flush=True)
            except Exception:
                print(f"[worker] unhandled error:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            if _running:
                time.sleep(args.poll_interval)
        print("[worker] daemon stopped", flush=True)
    else:
        n = process_all()
        print(f"[worker] finished — {n} item(s) processed")
