"""
Aaka — Google Tasks manager.
Uses the bot account token configured in aaka.yaml (auth block with tasks scope).
"""

import json, re, datetime, sys
from pathlib import Path

import os
BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config

TOKEN     = aaka_config.TOKENS_DIR / "token_aakash.json"
TASKLIST  = "@default"
TASKS_DIR = aaka_config.TASKS_DIR
CACHE_FILE = TASKS_DIR / "tasks_cache.json"
TASK_ORDER_FILE = TASKS_DIR / ".task_order.json"


def get_tasks_service(token_file=None):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token = Path(token_file) if token_file else TOKEN
    if not token.exists():
        raise FileNotFoundError(
            f"Token not found: {token}\n"
            f"{'Run: python3 admin/reauth.py --profile vps' if token_file else 'Run: ./reauth.py aakash'}"
        )
    creds = Credentials.from_authorized_user_file(token)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token.write_text(creds.to_json())
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _parse_title_meta(title: str) -> dict:
    """Extract metadata encoded inline in the task title."""
    starred   = title.startswith("⭐")
    urgent    = any(e in title for e in ("❗", "🔴", "🟥", "🔺"))
    recurring = "🔁" in title
    owner_match = re.search(r"@(\w+)", title)
    tags = re.findall(r"#\w+", title)
    owner = ""
    if owner_match:
        name = owner_match.group(1)
        for m in aaka_config.members():
            if m["name"].lower() == name.lower():
                owner = m["id"]
                break
    return {"starred": starred, "urgent": urgent, "recurring": recurring,
            "repeats": None, "owner": owner, "tags": tags}


def list_tasks(show_completed: bool = False, token_file=None) -> list[dict]:
    """Return open tasks (and optionally completed ones).

    Each dict has: id, title, notes, due, status.
    """
    svc = get_tasks_service(token_file=token_file)
    kwargs = {"tasklist": TASKLIST, "showCompleted": show_completed, "maxResults": 100}
    result = svc.tasks().list(**kwargs).execute()
    items = result.get("items", [])
    result = []
    for t in items:
        if not t.get("title"):
            continue
        notes = t.get("notes", "")
        result.append({
            "id":     t["id"],
            "title":  t.get("title", ""),
            "notes":  notes,
            "due":    t.get("due", ""),       # RFC 3339 or ""
            "status": t.get("status", ""),
            "_meta":  _parse_title_meta(t.get("title", "")),
        })
    return result


def create_task(title: str, notes: str = "", due_datetime: str = "", token_file=None) -> dict:
    """Create a new task. Returns the created task dict.

    due_datetime: RFC 3339 string, e.g. "2026-03-10T00:00:00.000Z"
    """
    svc = get_tasks_service(token_file=token_file)
    body: dict = {"title": title}
    if notes:
        body["notes"] = notes
    if due_datetime:
        body["due"] = due_datetime
    return svc.tasks().insert(tasklist=TASKLIST, body=body).execute()


def complete_task(task_id: str, token_file=None) -> dict:
    """Mark a task as completed. Returns the updated task dict."""
    svc = get_tasks_service(token_file=token_file)
    return svc.tasks().patch(
        tasklist=TASKLIST,
        task=task_id,
        body={"status": "completed"},
    ).execute()


def update_task_due(task_id: str, due_datetime: str, token_file=None) -> dict:
    """Update the due date of a task. due_datetime: RFC 3339 string e.g. '2026-03-10T00:00:00.000Z'"""
    svc = get_tasks_service(token_file=token_file)
    return svc.tasks().patch(
        tasklist=TASKLIST,
        task=task_id,
        body={"due": due_datetime},
    ).execute()


def delete_task(task_id: str, token_file=None) -> None:
    """Delete a task by ID."""
    svc = get_tasks_service(token_file=token_file)
    svc.tasks().delete(tasklist=TASKLIST, task=task_id).execute()


def find_task_by_title(title_query: str, token_file=None) -> dict | None:
    """Fuzzy-find the first open task whose title contains title_query (case-insensitive)."""
    q = title_query.lower()
    for t in list_tasks(token_file=token_file):
        if q in t["title"].lower():
            return t
    return None


def format_tasks_reply(tasks: list[dict]) -> str:
    """Format open tasks — sorted by due-date proximity, urgency, importance."""
    import datetime

    today = datetime.date.today()
    URGENCY_EMOJIS = ("🔴", "🟥", "🔺", "❗")

    pending = [t for t in tasks if t["status"] != "completed"]
    if not pending:
        return "All done 🌤️ No open tasks. You're on top of it! 🙌"

    def _get_due(t):
        raw = t.get("due", "")
        if not raw:
            return None
        try:
            return datetime.date.fromisoformat(raw[:10])
        except ValueError:
            return None

    def _is_urgent(t):
        meta = t.get("_meta", {})
        return bool(meta.get("urgent")) or any(e in t.get("title", "") for e in URGENCY_EMOJIS)

    def _is_starred(t):
        meta = t.get("_meta", {})
        return bool(meta.get("starred")) or "⭐" in t.get("title", "")

    def _due_label(due: datetime.date) -> tuple:
        delta = (due - today).days
        if delta <= 0:
            return "⏰", "Today"
        if delta == 1:
            return "⏰", "Tomorrow"
        if delta <= 3:
            return "⏰", due.strftime("%A")
        if delta <= 6:
            return "🕘", f"{due.strftime('%a')} ({due.strftime('%-d-%b')})"
        if delta <= 27:
            return "🕘", due.strftime("%-d-%b")
        if due.year == today.year:
            return "🕘", due.strftime("%B")
        return "🕘", str(due.year)

    def _line(t):
        title = t["title"]
        prefix = ""
        if _is_starred(t) and "⭐" not in title:
            prefix += "⭐"
        if _is_urgent(t) and not any(e in title for e in URGENCY_EMOJIS):
            prefix += "🔴"
        title_str = f"{prefix} {title}".strip() if prefix else title
        due = _get_due(t)
        if due:
            emoji, label = _due_label(due)
            due_str = f" {emoji} {label}"
        else:
            due_str = ""
        return f"{title_str}{due_str}"

    # Categorise
    sec1, sec2, sec3, sec4 = [], [], [], []
    for t in pending:
        due = _get_due(t)
        if due:
            delta = (due - today).days
            (sec1 if delta <= 3 else sec3).append(t)
        elif _is_urgent(t) and _is_starred(t):
            sec2.append(t)
        else:
            sec4.append(t)

    def _date_sort(lst):
        lst.sort(key=lambda t: (_get_due(t), not _is_urgent(t), not _is_starred(t)))

    def _prio_sort(lst):
        lst.sort(key=lambda t: (not _is_urgent(t), not _is_starred(t)))

    _date_sort(sec1)
    _prio_sort(sec2)
    _date_sort(sec3)
    _prio_sort(sec4)

    ordered = sec1 + sec2 + sec3 + sec4

    # Save display order so /prio, /urgent, /this_month, /next_month can reference by number
    try:
        TASKS_DIR.mkdir(exist_ok=True)
        TASK_ORDER_FILE.write_text(json.dumps([t["id"] for t in ordered]))
    except Exception:
        pass

    lines = [f"{len(pending)} task{'s' if len(pending) != 1 else ''} on the list:\n"]
    for i, t in enumerate(ordered, 1):
        lines.append(f"{i}. {_line(t)}")
    return "\n".join(lines)


def create_task_rich(task_json: dict, token_file=None) -> dict:
    """Create a task using rich PendingTask metadata.

    Encodes metadata inline in the title; notes = description + 🌤️ attribution.
    Returns the created Google Tasks API response dict.
    """
    clean_title = task_json["title"]
    description = task_json.get("description", "")
    due_date = task_json.get("due_date")

    # Build enriched title with inline markers
    prefix = ""
    if task_json.get("starred"):
        prefix += "⭐ "
    if task_json.get("urgent"):
        prefix += "❗ "

    suffix_parts = []
    owner_id = task_json.get("owner", "")
    if owner_id:
        owner_name = next(
            (m["name"] for m in aaka_config.members() if m["id"] == owner_id), ""
        )
        if owner_name:
            suffix_parts.append(f"@{owner_name}")
    for tag in task_json.get("tags", []):
        suffix_parts.append(tag)
    if task_json.get("recurring"):
        suffix_parts.append("🔁")

    title = prefix + clean_title
    if suffix_parts:
        title += " " + " ".join(suffix_parts)

    # Notes: description + duration marker + attribution on same line
    dur = task_json.get("duration", "")
    dur_marker = f"⏲️{dur}" if dur else "⏲️?m"
    notes = f"{description} {dur_marker} 🌤️" if description else f"{dur_marker} 🌤️"

    due_datetime = f"{due_date}T00:00:00.000Z" if due_date else ""
    return create_task(title, notes=notes, due_datetime=due_datetime, token_file=token_file)


def format_tasks_from_cache(entries: list[dict]) -> str:
    """Format tasks from cache JSON (no Google auth required — safe on sensor)."""
    import datetime
    today = datetime.date.today()
    pending = [t for t in entries if t.get("status", "needsAction") != "completed"]
    if not pending:
        return "All done 🌤️ No open tasks."

    def _get_due(t):
        raw = t.get("due_date", "")
        if not raw:
            return None
        try:
            return datetime.date.fromisoformat(raw[:10])
        except ValueError:
            return None

    def _due_label(due: datetime.date) -> str:
        delta = (due - today).days
        if delta <= 0:
            return "⏰ Today"
        if delta == 1:
            return "⏰ Tomorrow"
        if delta <= 3:
            return f"⏰ {due.strftime('%A')}"
        if delta <= 6:
            return f"🕘 {due.strftime('%a %-d %b')}"
        return f"🕘 {due.strftime('%-d %b')}"

    def _line(i: int, t: dict) -> str:
        prefix = ""
        if t.get("starred"):
            prefix += "⭐"
        if t.get("urgent"):
            prefix += "🔴"
        title = t.get("title", "")
        due = _get_due(t)
        due_str = f"  {_due_label(due)}" if due else ""
        tags = " ".join(t.get("tags", []))
        tags_str = f"  {tags}" if tags else ""
        return f"{i}. {prefix}{title}{due_str}{tags_str}".strip()

    # Sort: has-due first (ascending), then urgent/starred, then rest
    def _sort_key(t):
        due = _get_due(t)
        has_due = due is not None
        return (not has_due, due or datetime.date(9999, 1, 1), not t.get("urgent"), not t.get("starred"))

    pending.sort(key=_sort_key)
    lines = [f"🎯 *{len(pending)} task{'s' if len(pending) != 1 else ''}*\n"]
    for i, t in enumerate(pending, 1):
        lines.append(_line(i, t))
    lines.append(f"\n↪ /done <task> to complete")
    return "\n".join(lines)


def sync_tasks_cache(token_file=None) -> list[dict]:
    """Fetch all open tasks from Google Tasks, parse metadata, write cache.

    Returns list of enriched dicts.
    """
    task_list = list_tasks(token_file=token_file)
    synced_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = []
    for t in task_list:
        meta = _parse_title_meta(t["title"])
        notes = t.get("notes", "")
        # Parse duration marker from notes
        m_dur = re.search(r'⏲️(\S+)', notes)
        duration = m_dur.group(1) if m_dur else ""
        # Description: strip ⏲️ marker then trailing 🌤️ attribution
        description = re.sub(r'\s*⏲️\S+', '', notes).removesuffix(" 🌤️").removesuffix("🌤️").strip()

        due_date = ""
        raw_due = t.get("due", "")
        if raw_due:
            due_date = raw_due[:10]

        entries.append({
            "id":          t["id"],
            "title":       t["title"],
            "description": description,
            "starred":     meta.get("starred", False),
            "urgent":      meta.get("urgent", False),
            "priority_icon": "🔴" if meta.get("urgent") else ("🟡" if meta.get("starred") else ""),
            "tags":        meta.get("tags", []),
            "owner":       meta.get("owner", ""),
            "due_date":    due_date,
            "recurring":   meta.get("recurring", False),
            "repeats":     meta.get("repeats"),
            "list":        "My Tasks",
            "status":      t.get("status", "needsAction"),
            "duration":    duration,
            "synced_at":   synced_at,
        })

    TASKS_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2))
    return entries
