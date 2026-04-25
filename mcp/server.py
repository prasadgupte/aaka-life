#!/usr/bin/env python3
"""
mcp/server.py — Aaka MCP server for Claude Code

Exposes Aaka's skills as native MCP tools so Claude can act as the
conversational interface without going through Telegram/WhatsApp.
All tools call Python skills directly — no HTTP gateway required.

Start automatically via .mcp.json when running `claude` in this directory.
Or manually: venv/bin/python3 mcp/server.py
"""

import os
import sys
from pathlib import Path

# Ensure repo is on the path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("AAKA_CONFIG_DIR", str(Path.home() / ".aaka"))
os.environ.setdefault("AAKA_BASE", str(REPO))

import aaka_config

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("mcp package not installed — run: venv/bin/pip install mcp", file=sys.stderr)
    sys.exit(1)

_name = aaka_config.bot_name()
mcp = FastMCP(_name)


# ── Schedule ──────────────────────────────────────────────────────────────────

@mcp.tool()
def today_schedule() -> str:
    """Return today's calendar events."""
    path = aaka_config.CALENDAR_DIR / "today.md"
    if path.exists():
        return path.read_text()
    return "No calendar data yet — run `bash admin/cal.sh` to sync."


@mcp.tool()
def weekly_schedule() -> str:
    """Return this week's calendar events."""
    path = aaka_config.CALENDAR_DIR / "weekly.md"
    if path.exists():
        return path.read_text()
    return "No calendar data yet — run `bash admin/cal.sh` to sync."


@mcp.tool()
def add_event(
    title: str,
    date: str,
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    calendar_id: str = "",
) -> dict:
    """Add an event to Google Calendar.

    Args:
        title: Event title.
        date: Date in YYYY-MM-DD format.
        start_time: Optional start time as HH:MM (24h). Omit for all-day.
        end_time: Optional end time as HH:MM. Defaults to start_time + 1h.
        description: Optional notes.
        calendar_id: Target calendar ID. Defaults to the primary calendar.
    """
    from skills.calendar.gog import create_event
    result = create_event(
        title=title,
        date=date,
        start_time=start_time,
        end_time=end_time,
        description=description,
        calendar_id=calendar_id or aaka_config.calendar_id(),
    )
    return {"ok": True, "event_id": result["id"], "link": result.get("link", "")}


# ── Tasks ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_tasks(filter: str = "week") -> str:
    """List open tasks.

    Args:
        filter: One of: week (default), today, overdue, all, inbox.
    """
    from skills.tasks.local_tasks import list_open, format_tasks
    tasks = list_open(due_filter=filter)
    if not tasks:
        return f"No open tasks for filter '{filter}'."
    return format_tasks(tasks)


@mcp.tool()
def add_task(
    title: str,
    due_date: str = "",
    owner: str = "",
    priority: str = "",
    tags: list[str] | None = None,
) -> dict:
    """Add a task to the local task store.

    Args:
        title: Task title.
        due_date: Optional due date as YYYY-MM-DD.
        owner: Optional member id (e.g. 'alice', 'bob').
        priority: Optional: high, medium, low.
        tags: Optional list of tag strings.
    """
    from skills.tasks.local_tasks import add_task as _add
    payload = {
        "title": title,
        "due_date": due_date,
        "owner": owner or aaka_config.default_actor(),
        "priority": priority,
        "tags": tags or [],
        "namespace": owner or aaka_config.default_actor(),
    }
    result = _add(payload)
    return {"ok": True, "task_id": result.get("id", ""), "title": result.get("title", title)}


@mcp.tool()
def complete_task(task_num: int) -> dict:
    """Mark a task as done by its display number (from list_tasks).

    Args:
        task_num: The task number shown in list_tasks output.
    """
    from skills.tasks.local_tasks import complete_task_by_num
    result = complete_task_by_num(task_num)
    return {"ok": result.get("ok", False), "message": result.get("message", str(result))}


# ── Email ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_emails(
    label: str = "INBOX",
    member_id: str = "",
    max_results: int = 10,
) -> list[dict]:
    """Fetch emails from Gmail.

    Args:
        label: Gmail label name (e.g. 'INBOX', 'Travel/AAKA-JP').
        member_id: Family member id whose Gmail to read. Defaults to admin.
        max_results: Maximum number of emails to return (max 20).
    """
    from skills.mail.gmail import get_gmail_service, list_by_label, fetch_full
    mid = member_id or aaka_config.default_actor()
    svc = get_gmail_service(mid)
    msgs = list_by_label(svc, label, max_results=min(max_results, 20))
    result = []
    for m in msgs:
        try:
            full = fetch_full(svc, m["id"])
            result.append({"id": m["id"], **full})
        except Exception:
            continue
    return result


@mcp.tool()
def save_email_attachment(
    message_id: str,
    destination_folder: str,
    member_id: str = "",
) -> dict:
    """Download all attachments from a Gmail message and save them to a folder.

    Args:
        message_id: Gmail message id (from list_emails).
        destination_folder: Absolute path or vault-relative path to save files.
        member_id: Family member id whose Gmail to read. Defaults to admin.
    """
    from skills.mail.gmail import get_gmail_service, fetch_attachments
    import base64
    mid = member_id or aaka_config.default_actor()
    svc = get_gmail_service(mid)
    attachments = fetch_attachments(svc, message_id)

    dest = Path(destination_folder).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    saved = []
    for att in attachments:
        if not att.get("data_base64"):
            # Fetch with data
            from skills.mail.gmail import fetch_attachments as _fa
            # Re-fetch with data inline
            pass
        fname = att.get("filename", f"attachment_{len(saved)}")
        data = base64.urlsafe_b64decode(att.get("data_base64", "") + "==")
        out_path = dest / fname
        out_path.write_bytes(data)
        saved.append(str(out_path))

    return {"ok": True, "saved": saved, "count": len(saved)}


# ── Notes & Lists ─────────────────────────────────────────────────────────────

@mcp.tool()
def add_note(topic: str, body: str, member_id: str = "") -> dict:
    """Append a note to a topic log.

    Args:
        topic: Topic name (e.g. 'ideas', 'health', 'shopping').
        body: The note content.
        member_id: Optional member namespace. Defaults to admin.
    """
    from skills.notes.note_writer import append_note
    namespace = member_id or aaka_config.default_actor()
    result = append_note(topic=topic, body=body, namespace=namespace)
    return {"ok": True, "path": str(result.get("path", ""))}


@mcp.tool()
def shopping_list(
    list_name: str,
    action: str = "show",
    items: list[str] | None = None,
    member_id: str = "",
) -> str:
    """View or update a shopping / to-buy list.

    Args:
        list_name: List name (e.g. 'groceries', 'pharmacy').
        action: 'show' to display, 'add' to add items, 'done' to mark all done.
        items: Items to add (only used when action='add').
        member_id: Optional member namespace.
    """
    from skills.lists.list_manager import show, add_items, done_all
    ns = member_id or aaka_config.default_actor()
    if action == "add" and items:
        result = add_items(list_name, items, namespace=ns)
        return f"Added {result.get('added', 0)} item(s) to {list_name}.\n\n" + show(list_name, ns)
    elif action == "done":
        return done_all(list_name, ns)
    else:
        return show(list_name, ns)


# ── System ────────────────────────────────────────────────────────────────────

@mcp.tool()
def setup_status() -> dict:
    """Return the current setup status across all tiers (0–3).

    Use this to check what's configured and what's missing before
    attempting calendar, email, or other operations.
    """
    import subprocess, json
    result = subprocess.run(
        [sys.executable, str(REPO / "admin" / "setup_check.py"), "--json"],
        capture_output=True, text=True,
        env={**os.environ, "AAKA_BASE": str(REPO)},
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    return {"error": result.stderr[:500]}


@mcp.tool()
def bot_info() -> dict:
    """Return the assistant's name, members, and timezone."""
    return {
        "name": aaka_config.bot_name(),
        "emoji": aaka_config.bot_emoji(),
        "timezone": aaka_config.timezone(),
        "members": [
            {"id": m["id"], "name": m.get("name", m["id"]), "role": m.get("role", "")}
            for m in aaka_config.members()
        ],
    }


if __name__ == "__main__":
    mcp.run()
