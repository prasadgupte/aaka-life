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
def security_check() -> dict:
    """Run the security self-audit (admin/security_check.py) and return results.

    Read-only: inspects file permissions, git tracking, and code structure for
    known exposure classes (secrets in git, loose perms, inbound ports,
    shell=True, missing admin gates). Run before deploying the sensor.
    """
    import subprocess, json
    result = subprocess.run(
        [sys.executable, str(REPO / "admin" / "security_check.py"), "--json"],
        capture_output=True, text=True,
        env={**os.environ, "AAKA_BASE": str(REPO)},
    )
    if result.returncode == 0 and result.stdout.strip():
        return json.loads(result.stdout)
    return {"error": (result.stderr or "no output")[:500]}


@mcp.tool()
def exposure_report() -> dict:
    """Attack-surface inventory (admin/exposure_report.py): listening ports (and
    which are network-reachable), public VPS endpoints, each OAuth token's real
    capability (calendar write, gmail send, …), and the secrets/scraping-cred
    inventory (names only). Read-only. Complements security_check()."""
    import subprocess, json
    script = REPO / "admin" / "exposure_report.py"
    if not script.exists():
        return {"error": "exposure_report.py is a local-only ops tool, not bundled with aaka."}
    result = subprocess.run(
        [sys.executable, str(script), "--json"],
        capture_output=True, text=True,
        env={**os.environ, "AAKA_BASE": str(REPO)},
    )
    if result.returncode == 0 and result.stdout.strip():
        return json.loads(result.stdout)
    return {"error": (result.stderr or "no output")[:500]}


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


# ── Config management (no aaka.yaml editing — dynamic stores) ────────────────
# These let Claude manage members + contacts by conversation. Everything lands
# in data/members_dynamic.json + data/wa_allowlist.json, merged by aaka_config —
# aaka.yaml is never mutated, so there's no formatting/comment risk.

@mcp.tool()
def list_members() -> dict:
    """List everyone the assistant knows: id, name, role, admin, source
    (yaml|invite), and the WhatsApp/Telegram/Signal handles bound to each."""
    from sensor import wa_onboard
    out = []
    for m in aaka_config.members():
        out.append({
            "id": m.get("id"),
            "name": m.get("name", m.get("id")),
            "role": m.get("role", ""),
            "admin": bool(m.get("admin")),
            "source": m.get("source", "yaml"),
            "whatsapp_field": aaka_config.member_handle(m, "whatsapp"),
            "telegram_field": aaka_config.member_handle(m, "telegram"),
            "signal_field": aaka_config.member_handle(m, "signal"),
            "bound_handles": wa_onboard.handles_for(m.get("id", "")),
        })
    return {"members": out}


@mcp.tool()
def add_member(name: str) -> dict:
    """Create a new member by name (no aaka.yaml editing). Returns the member.
    Use invite() next to bind their WhatsApp, or set_contact() if you know it."""
    m = aaka_config.add_dynamic_member(name)
    return {"ok": True, "member": m}


@mcp.tool()
def set_contact(member: str, handle: str, channel: str = "whatsapp") -> dict:
    """Bind a contact handle to an existing member (id or name) so they're
    recognized — without editing aaka.yaml. `handle` is a WhatsApp @lid/+E.164,
    a Telegram id, or a Signal +E.164/uuid. `channel` (whatsapp|telegram|signal)
    is informational — the allowlist is generic and matches any handle."""
    from sensor import wa_onboard
    a = member.strip().lower()
    target = next((m for m in aaka_config.members()
                   if m.get("id", "").lower() == a or m.get("name", "").lower() == a), None)
    if not target:
        return {"ok": False, "error": f"no member '{member}' — add_member() first"}
    wa_onboard.bind(handle, target["id"])
    return {"ok": True, "member": target["id"], "handle": handle.strip().lower(), "channel": channel}


@mcp.tool()
def invite(name: str, channel: str = "") -> dict:
    """Create a one-time invite for a member (creating the member if new) and
    return the deep links the invitee taps to auto-register. No config editing.

    Links are returned for every channel that can build one; `channel`
    (whatsapp|telegram|signal) is an optional hint recorded in the result — the
    same code works on whichever channel they actually message from. Signal
    deep links cannot pre-fill text, so the invitee sends `prefilled_text`
    themselves."""
    from sensor import wa_onboard
    a = name.strip().lower()
    target = next((m for m in aaka_config.members()
                   if m.get("id", "").lower() == a or m.get("name", "").lower() == a), None)
    created = False
    if not target:
        target = aaka_config.add_dynamic_member(name)
        created = True
    nm = target.get("name", name)
    code = wa_onboard.create_invite(target["id"], nm)
    url, text = wa_onboard.invite_link(code, nm)
    sg_url, sg_text = wa_onboard.signal_invite_link(code, nm)
    return {"ok": True, "member": target["id"], "created": created,
            "code": code, "channel": channel or "any",
            "wa_me_link": url, "tg_link": wa_onboard.tg_invite_link(code, nm),
            "signal_link": sg_url, "signal_text": sg_text,
            "prefilled_text": text}


@mcp.tool()
def remove_member(member_id: str) -> dict:
    """Remove a dynamically-created member and unbind their handles. Only removes
    members created via onboarding — never touches aaka.yaml members."""
    from sensor import wa_onboard
    unbound = wa_onboard.unbind_member(member_id)
    removed = aaka_config.remove_dynamic_member(member_id)
    if not removed:
        return {"ok": False, "error": f"'{member_id}' is not a dynamic member "
                f"(aaka.yaml members are not removable here); unbound {unbound} handle(s)"}
    return {"ok": True, "removed": member_id, "handles_unbound": unbound}


# ── aaka Tools (pluggable scheduled/triggered capabilities) ──────────────────
# See docs/aaka-tools.md. Tools run a script, report back through aaka, and are
# managed by manifest (config/tools.yaml) — admin-only, no hand-editing.

@mcp.tool()
def list_tools() -> dict:
    """List registered aaka Tools with schedule, command, placement, last run."""
    from sensor import tool_runner
    return {"tools": tool_runner.status()}


@mcp.tool()
def register_tool(name: str, run: str, schedule: str = "", command: str = "",
                  placement: str = "executor", report_to: str = "",
                  on_error: str = "alert", secrets: str = "", args: str = "") -> dict:
    """Register/update an aaka Tool. `run` is a script path; `schedule` is cron
    (blank = on-demand only); `secrets` names a vault dir under
    $AAKA_CONFIG_DIR/secrets/. Admin action — persists to config/tools.yaml."""
    from sensor import tool_runner
    entry = tool_runner.register(
        name, run, schedule=schedule or None, command=command or None,
        placement=placement, report_to=report_to or None,
        on_error=on_error, secrets=secrets or None, args=args or None)
    return {"ok": True, "tool": name, "entry": entry}


@mcp.tool()
def add_reminder(text: str, member: str = "", weekday: str = "",
                 event_matches: str = "", unless_matches: str = "") -> dict:
    """Add a recurring contextual reminder shown in that day's schedule and
    morning brief.

    Give it a condition — a weekday, something today's calendar contains, or
    both:

      weekday        "mon" or "mon,wed" — plain calendar weekday.
      event_matches  regex over today's events, e.g. "sport|PE". PREFER THIS
                     when the reminder depends on an activity: it follows the
                     timetable, so it keeps working when sport moves day.
      unless_matches regex that suppresses it, e.g. "holiday|no school".

    `member` scopes it to one person's day view (their member id); omit for the
    whole household. Reminders are NOT tasks: nothing to tick, nothing goes
    overdue. Use add_task for anything you want to track to completion."""
    from skills.reminders import rules
    try:
        res = rules.add(text, member=member, weekday=weekday,
                        event_matches=event_matches, unless_matches=unless_matches)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **res}


@mcp.tool()
def list_reminders() -> dict:
    """All configured reminders with their ids, conditions and enabled state."""
    from skills.reminders import rules
    return {"ok": True, "reminders": rules.load()}


@mcp.tool()
def remove_reminder(reminder_id: str) -> dict:
    """Delete a reminder by id (see list_reminders)."""
    from skills.reminders import rules
    return {"ok": rules.remove(reminder_id), "id": reminder_id}


@mcp.tool()
def set_reminder_enabled(reminder_id: str, enabled: bool) -> dict:
    """Mute or unmute a reminder without deleting it — e.g. over the holidays."""
    from skills.reminders import rules
    return {"ok": rules.set_enabled(reminder_id, enabled),
            "id": reminder_id, "enabled": enabled}


@mcp.tool()
def preview_reminders(date: str = "", member: str = "") -> dict:
    """What would fire on `date` (YYYY-MM-DD, default today) against that day's
    real calendar. Use this to check a rule before trusting it — a reminder on
    the wrong day is worse than none, because it costs trust in all of them."""
    import datetime as _dt
    from skills.reminders import rules
    import aaka_config as _cfg
    try:
        when = _dt.date.fromisoformat(date) if date else _dt.date.today()
    except ValueError:
        return {"ok": False, "error": f"bad date {date!r}, expected YYYY-MM-DD"}
    cal = _cfg.CALENDAR_DIR
    path = cal / f"today_{member}.md" if member else cal / "today.md"
    if not path.exists():
        path = cal / "today.md"
    raw = path.read_text() if path.exists() else ""
    return {"ok": True, "date": when.isoformat(), "member": member or "(all)",
            "would_fire": rules.due(raw, date=when, member=member),
            "calendar_available": bool(raw)}


@mcp.tool()
def register_watchdog(name: str, watch: str, schedule: str, recipients: str = "",
                      reminder: str = "", placement: str = "sensor") -> dict:
    """Register a dead-man's-switch. If signal `watch` has no heartbeat for today by
    the time `schedule` fires, remind `recipients` (comma-separated member ids) via
    Telegram. `schedule` is 5-field cron — use the day-of-week field to avoid weekend
    noise, e.g. '30 20 * * 1-5' = weekday 20:30. Runs on the sensor (placement default)
    so it fires even when the Mac is off. Unregister with remove_tool."""
    from sensor import tool_runner
    recs = [r.strip() for r in recipients.split(",") if r.strip()]
    entry = tool_runner.register(
        name, run="", kind="watchdog", watch=watch, schedule=schedule,
        placement=placement, recipients=recs or None, reminder=reminder or None,
        report_to=(recs[0] if recs else None))
    return {"ok": True, "watchdog": name, "watch": watch, "entry": entry}


@mcp.tool()
def run_tool(name: str, args: str = "") -> dict:
    """Run a registered tool now and report back. Returns its structured result."""
    from sensor import tool_runner
    return tool_runner.run_and_report(name, args)


@mcp.tool()
def set_tool_enabled(name: str, enabled: bool) -> dict:
    """Enable or disable a tool without removing it."""
    from sensor import tool_runner
    return {"ok": tool_runner.set_enabled(name, enabled), "tool": name, "enabled": enabled}


@mcp.tool()
def remove_tool(name: str) -> dict:
    """Unregister a tool (poller/watchdog) — removes it from the manifest so it
    no longer runs or schedules. The clean counterpart to register_tool."""
    from sensor import tool_runner
    return {"ok": tool_runner.remove(name), "removed": name}


@mcp.tool()
def tool_logs(name: str, n: int = 10) -> dict:
    """Recent run records for a tool (start ts, ok, error, summary, ms)."""
    from sensor import tool_runner
    return {"tool": name, "runs": tool_runner.logs_tail(name, n)}


if __name__ == "__main__":
    mcp.run()
