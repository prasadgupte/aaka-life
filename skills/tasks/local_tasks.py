"""
skills/tasks/local_tasks.py — Local JSON task store.

Storage: $AAKA_CONFIG_DIR/data/tasks/tasks.json
Zero external dependencies — no Google API, no token, works on sensor and executor.

Public API:
  add_task(payload)           → dict (task record)
  complete_task_by_id(id)     → dict
  complete_task_by_num(n)     → dict
  complete_tasks_by_nums(ns)  → list[dict]
  snooze_task(id, days)       → dict
  snooze_all(days, filter)    → int (count snoozed)
  list_open(owner, tag, due_filter) → list[dict]
  list_completed(since)       → list[dict]
  find_by_title(query)        → dict | None
  format_tasks(tasks, label)  → str
  summary_for_push()          → str
  update_task_by_num(n, upd)  → dict
  delete_task_by_num(n)       → dict
  recreate_recurring(task)    → dict | None
  save_display_order(tasks)   → None
"""

import datetime, json, os, re, uuid
from pathlib import Path


def _now() -> str:
    """Current UTC datetime as ISO string with second-level precision."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

import sys
BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config


# ── Paths ──────────────────────────────────────────────────────────────────────

def _tasks_file() -> Path:
    d = aaka_config.TASKS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "tasks.json"

def _order_file() -> Path:
    return aaka_config.TASKS_DIR / ".task_order.json"


# ── Low-level read/write ───────────────────────────────────────────────────────

def _load() -> list[dict]:
    p = _tasks_file()
    if not p.exists():
        return []
    try:
        tasks = json.loads(p.read_text())
    except Exception:
        return []
    # Lazy backfill: stamp updated_at on tasks that predate this field.
    changed = False
    for t in tasks:
        if not t.get("updated_at"):
            t["updated_at"] = t.get("done_at") or t.get("created_at") or _now()
            changed = True
    if changed:
        try:
            _tasks_file().write_text(json.dumps(tasks, ensure_ascii=False, indent=2))
        except Exception:
            pass
    return tasks

def _save(tasks: list[dict]) -> None:
    _tasks_file().write_text(json.dumps(tasks, ensure_ascii=False, indent=2))


# ── Filters ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.date.today().isoformat()

def _is_visible(t: dict) -> bool:
    """A task is visible if open (not done/deleted) and not snoozed past today."""
    if t.get("status") not in ("open", None):
        return False
    snooze = t.get("snooze_until")
    if snooze and snooze > _today():
        return False
    return True

def list_open(owner: str = "", tag: str = "", due_filter: str = "") -> list[dict]:
    """List open visible tasks with optional filters.

    due_filter: "today", "overdue", "week", or "" (all).
    """
    tasks = _load()
    visible = [t for t in tasks if _is_visible(t)]
    if owner:
        visible = [t for t in visible if not t.get("owner") or t.get("owner") == owner]
    if tag:
        tag_lower = tag.lower()
        visible = [t for t in visible if any(tag_lower in tg.lower() for tg in t.get("tags", []))]
    if due_filter:
        today = datetime.date.today()
        if due_filter == "today":
            visible = [t for t in visible if t.get("due_date") == today.isoformat()]
        elif due_filter == "overdue":
            visible = [t for t in visible if t.get("due_date") and t["due_date"] < today.isoformat()]
        elif due_filter == "week":
            end = (today + datetime.timedelta(days=7)).isoformat()
            visible = [t for t in visible if t.get("due_date") and t["due_date"] <= end]
        elif due_filter == "nodate":
            visible = [t for t in visible if not t.get("due_date")]
    return visible

def list_snoozed() -> list[dict]:
    today = _today()
    return [t for t in _load() if t.get("status") == "open" and t.get("snooze_until", "") > today]


# ── Writes ────────────────────────────────────────────────────────────────────

def add_task(payload: dict) -> dict:
    """Add a task from an LLM-extracted payload. Returns the new task record."""
    tasks = _load()
    now = _now()
    task = {
        "id":           uuid.uuid4().hex[:8],
        "title":        payload.get("title", ""),
        "owner":        payload.get("owner", ""),
        "due_date":     payload.get("due_date", ""),
        "priority":     "high" if payload.get("urgent") else ("medium" if payload.get("starred") else "normal"),
        "snooze_until": None,
        "tags":         payload.get("tags", []),
        "recurring":    payload.get("recurring", False),
        "repeats":      payload.get("repeats", ""),
        "description":  payload.get("description", ""),
        # Provenance — all optional; set when an agent/skill creates the task
        "project":      payload.get("project") or None,   # entity/project slug, e.g. "japan26"
        "agent":        payload.get("agent") or None,     # agent id, e.g. "travel"
        "skill":        payload.get("skill") or None,     # skill path, e.g. "book_flight"
        "created_at":   now,
        "updated_at":   now,
        "done_at":      None,
        "status":       "open",
    }
    tasks.append(task)
    _save(tasks)
    return task


def _complete(task: dict) -> dict:
    now = _now()
    task["status"]     = "done"
    task["done_at"]    = now
    task["updated_at"] = now
    return task


def delete_task(task_id: str) -> dict:
    """Mark a task as deleted by ID. Returns the task dict.

    Uses soft-delete (status='deleted') so the deletion propagates to the other
    side via sync merge. Hard removal happens during archival after 7 days.
    """
    tasks = _load()
    for t in tasks:
        if t["id"] == task_id:
            t["status"]     = "deleted"
            t["updated_at"] = _now()
            _save(tasks)
            return t
    raise ValueError(f"Task {task_id!r} not found")


def complete_task_by_id(task_id: str) -> dict:
    tasks = _load()
    for t in tasks:
        if t["id"] == task_id and t.get("status") in ("open", None):
            _complete(t)
            _save(tasks)
            return t
    raise ValueError(f"Task {task_id!r} not found or already done")


def complete_task_by_num(n: int) -> dict:
    """Complete by 1-based display number from last list_open() output."""
    order_file = _order_file()
    if not order_file.exists():
        raise ValueError("No task list yet — run /tasks first")
    ids = json.loads(order_file.read_text())
    idx = n - 1
    if idx < 0 or idx >= len(ids):
        raise ValueError(f"Task #{n} not in list (only {len(ids)} tasks)")
    return complete_task_by_id(ids[idx])


def snooze_task(task_id: str, days: int) -> dict:
    tasks = _load()
    for t in tasks:
        if t["id"] == task_id and t.get("status") == "open":
            until = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
            t["snooze_until"] = until
            t["updated_at"]   = _now()
            _save(tasks)
            return t
    raise ValueError(f"Task {task_id!r} not found")


def snooze_task_by_num(n: int, days: int) -> dict:
    order_file = _order_file()
    if not order_file.exists():
        raise ValueError("No task list yet — run /tasks first")
    ids = json.loads(order_file.read_text())
    idx = n - 1
    if idx < 0 or idx >= len(ids):
        raise ValueError(f"Task #{n} not in list")
    return snooze_task(ids[idx], days)


def find_by_title(query: str) -> dict | None:
    q = query.lower()
    for t in list_open():
        if q in t["title"].lower():
            return t
    return None


def save_display_order(tasks: list[dict]) -> None:
    _order_file().write_text(json.dumps([t["id"] for t in tasks]))


def title_for_num(n: int) -> str:
    order_file = _order_file()
    if not order_file.exists():
        return f"task #{n}"
    try:
        ids = json.loads(order_file.read_text())
        idx = n - 1
        if 0 <= idx < len(ids):
            task_id = ids[idx]
            tasks = _load()
            t = next((t for t in tasks if t["id"] == task_id), None)
            return t["title"] if t else f"task #{n}"
    except Exception:
        pass
    return f"task #{n}"


# ── Formatting ────────────────────────────────────────────────────────────────

def _due_label(due_str: str) -> str:
    try:
        due = datetime.date.fromisoformat(due_str)
    except (ValueError, TypeError):
        return ""
    delta = (due - datetime.date.today()).days
    if delta < 0:
        return f"⏰ {abs(delta)}d overdue"
    if delta == 0:
        return "⏰ Today"
    if delta == 1:
        return "⏰ Tomorrow"
    if delta <= 3:
        return f"⏰ {due.strftime('%A')}"
    if delta <= 6:
        return f"🕘 {due.strftime('%a %-d %b')}"
    if delta <= 27:
        return f"🕘 {due.strftime('%-d %b')}"
    return f"🕘 {due.strftime('%-d %b %Y')}"


def _priority_icon(t: dict) -> str:
    p = t.get("priority", "normal")
    if p == "high":
        return "🔴"
    if p == "medium":
        return "⭐"
    return ""


def format_tasks(tasks: list[dict], label: str = "") -> str:
    if not tasks:
        suffix = f" ({label})" if label else ""
        return f"All done 🌤️ No open tasks{suffix}."

    today = datetime.date.today()

    def _sort_key(t):
        due = t.get("due_date") or ""
        has_due = bool(due)
        try:
            due_d = datetime.date.fromisoformat(due) if due else datetime.date(9999, 1, 1)
        except ValueError:
            due_d = datetime.date(9999, 1, 1)
        prio_order = {"high": 0, "medium": 1, "normal": 2}.get(t.get("priority", "normal"), 2)
        return (not has_due, due_d, prio_order)

    ordered = sorted(tasks, key=_sort_key)
    save_display_order(ordered)

    header_label = f" ({label})" if label else ""
    lines = [f"🎯 *{len(ordered)} task{'s' if len(ordered) != 1 else ''}*{header_label}\n"]
    for i, t in enumerate(ordered, 1):
        icon = _priority_icon(t)
        title = t.get("title", "")
        due_str = f"  {_due_label(t['due_date'])}" if t.get("due_date") else ""
        # Show provenance tag: project first, else agent (skip "aaka" — that's implicit)
        project = t.get("project") or ""
        agent   = t.get("agent") or ""
        prov    = project or (agent if agent != "aaka" else "")
        prov_str = f"  [{prov}]" if prov else ""
        tags = " ".join(tag for tag in (t.get("tags") or []) if not tag.startswith("#agent:"))
        tags_str = f"  {tags}" if tags else ""
        owner = t.get("owner", "")
        owner_str = f"  @{owner}" if owner else ""
        lines.append(f"{i}. {icon}{title}{due_str}{prov_str}{tags_str}{owner_str}".strip())

    lines.append("\n↪ /done N · /snooze N 3d · /edit N")
    return "\n".join(lines)


# ── Bulk & advanced operations ────────────────────────────────────────────────

def complete_tasks_by_nums(nums: list[int]) -> list[dict]:
    """Complete multiple tasks by 1-based display numbers. Returns list of completed tasks."""
    order_file = _order_file()
    if not order_file.exists():
        raise ValueError("No task list yet — run /tasks first")
    ids = json.loads(order_file.read_text())
    tasks = _load()
    completed = []
    for n in sorted(set(nums)):
        idx = n - 1
        if idx < 0 or idx >= len(ids):
            continue
        task_id = ids[idx]
        for t in tasks:
            if t["id"] == task_id and t.get("status") == "open":
                _complete(t)
                completed.append(t)
                break
    if completed:
        _save(tasks)
    return completed


def delete_task_by_num(n: int) -> dict:
    """Delete task by 1-based display number."""
    order_file = _order_file()
    if not order_file.exists():
        raise ValueError("No task list yet — run /tasks first")
    ids = json.loads(order_file.read_text())
    idx = n - 1
    if idx < 0 or idx >= len(ids):
        raise ValueError(f"Task #{n} not in list (only {len(ids)} tasks)")
    return delete_task(ids[idx])


def update_task_by_id(task_id: str, updates: dict) -> dict:
    """Patch a task by ID. Allowed fields: title, owner, due_date, priority, tags,
    description, project, agent, skill, recurring, repeats."""
    allowed = {"title", "owner", "due_date", "priority", "tags",
               "description", "project", "agent", "skill", "recurring", "repeats",
               "snooze_until"}
    tasks = _load()
    for t in tasks:
        if t["id"] == task_id:
            old = dict(t)
            for k, v in updates.items():
                if k in allowed:
                    t[k] = v
            t["updated_at"] = _now()
            _save(tasks)
            return {"task": t, "old": old}
    raise ValueError(f"Task {task_id!r} not found")


def update_task_by_num(n: int, updates: dict) -> dict:
    """Patch a task by display number. updates can include: due_date, owner, priority, title."""
    order_file = _order_file()
    if not order_file.exists():
        raise ValueError("No task list yet — run /tasks first")
    ids = json.loads(order_file.read_text())
    idx = n - 1
    if idx < 0 or idx >= len(ids):
        raise ValueError(f"Task #{n} not in list (only {len(ids)} tasks)")
    task_id = ids[idx]
    tasks = _load()
    for t in tasks:
        if t["id"] == task_id:
            old = dict(t)
            for k, v in updates.items():
                if k in ("due_date", "owner", "priority", "title", "tags"):
                    t[k] = v
            t["updated_at"] = _now()
            _save(tasks)
            return {"task": t, "old": old}
    raise ValueError(f"Task #{n} not found")


def list_completed(since: str = "today") -> list[dict]:
    """List completed tasks since a given period: 'today', 'yesterday', 'week'."""
    today = datetime.date.today()
    if since == "today":
        cutoff = today.isoformat()
    elif since == "yesterday":
        cutoff = (today - datetime.timedelta(days=1)).isoformat()
    elif since == "week":
        cutoff = (today - datetime.timedelta(days=7)).isoformat()
    else:
        cutoff = today.isoformat()
    tasks = _load()
    done = [t for t in tasks if t.get("status") == "done" and (t.get("done_at") or "") >= cutoff]
    return sorted(done, key=lambda t: t.get("done_at", ""))


def snooze_all(days: int, due_filter: str = "all") -> int:
    """Snooze all visible tasks (or only overdue). Returns count snoozed."""
    tasks = _load()
    today = datetime.date.today()
    until = (today + datetime.timedelta(days=days)).isoformat()
    count = 0
    for t in tasks:
        if not _is_visible(t):
            continue
        if due_filter == "overdue" and not (t.get("due_date") and t["due_date"] < today.isoformat()):
            continue
        t["snooze_until"] = until
        t["updated_at"]   = _now()
        count += 1
    if count:
        _save(tasks)
    return count


def summary_for_push() -> str:
    """Compact task summary for the morning push — overdue + due today + stale undated (max 8 items)."""
    today = datetime.date.today()
    today_iso = today.isoformat()
    stale_cutoff = (today - datetime.timedelta(days=7)).isoformat()
    tasks = _load()
    visible = [t for t in tasks if _is_visible(t)]
    overdue = [t for t in visible if t.get("due_date") and t["due_date"] < today_iso]
    due_today = [t for t in visible if t.get("due_date") == today_iso]
    stale_undated = [t for t in visible if not t.get("due_date") and (t.get("created_at") or today_iso) <= stale_cutoff]

    if not overdue and not due_today and not stale_undated:
        return ""

    items = sorted(overdue, key=lambda t: t.get("due_date", "")) + due_today
    items = items[:8]

    n_overdue = len(overdue)
    n_today = len(due_today)
    n_stale = len(stale_undated)
    parts = []
    if n_today:
        parts.append(f"{n_today} due today")
    if n_overdue:
        parts.append(f"{n_overdue} overdue")
    if n_stale:
        parts.append(f"{n_stale} undated")
    header = f"📋 {' · '.join(parts)}"

    lines = [header]
    for t in items:
        icon = _priority_icon(t)
        title = t.get("title", "")
        due_lbl = _due_label(t.get("due_date", ""))
        lines.append(f"  {icon}{title}  {due_lbl}".strip())

    # Show up to 3 stale undated tasks as a nudge
    if stale_undated and len(items) < 8:
        for t in stale_undated[:3]:
            icon = _priority_icon(t)
            age = (today - datetime.date.fromisoformat(t.get("created_at", today_iso))).days
            lines.append(f"  {icon}{t.get('title', '')}  📥 {age}d old, no date")

    lines.append("↪ /done N · /tasks · /edit N <date>")
    return "\n".join(lines)


def recreate_recurring(task: dict) -> dict | None:
    """Clone a recurring task with the next due date. Returns new task or None."""
    if not task.get("recurring"):
        return None

    repeats = (task.get("repeats") or "").lower().strip()
    delta = _parse_recurrence(repeats)
    if not delta:
        return None

    today = datetime.date.today()
    old_due = task.get("due_date")
    if old_due:
        try:
            base = datetime.date.fromisoformat(old_due)
            # Advance from old due date; if in the past, advance from today
            new_due = base + delta
            while new_due <= today:
                new_due += delta
        except ValueError:
            new_due = today + delta
    else:
        new_due = today + delta

    now = _now()
    new_task = {
        "id":           uuid.uuid4().hex[:8],
        "title":        task.get("title", ""),
        "owner":        task.get("owner", ""),
        "due_date":     new_due.isoformat(),
        "priority":     task.get("priority", "normal"),
        "snooze_until": None,
        "tags":         list(task.get("tags", [])),
        "recurring":    True,
        "repeats":      task.get("repeats", ""),
        "description":  task.get("description", ""),
        "created_at":   now,
        "updated_at":   now,
        "done_at":      None,
        "status":       "open",
    }
    tasks = _load()
    tasks.append(new_task)
    _save(tasks)
    return new_task


def archive_done_tasks(done_days: int = 30, deleted_days: int = 7) -> int:
    """Move old done/deleted tasks to tasks_archive.json. Returns count moved."""
    tasks = _load()
    cutoff_done = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=done_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_del  = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=deleted_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    keep, archive = [], []
    for t in tasks:
        status = t.get("status", "open")
        ts = t.get("updated_at") or t.get("done_at") or t.get("created_at") or ""
        if status == "done" and ts and ts < cutoff_done:
            archive.append(t)
        elif status == "deleted" and ts and ts < cutoff_del:
            archive.append(t)
        else:
            keep.append(t)

    if not archive:
        return 0

    archive_file = aaka_config.TASKS_DIR / "tasks_archive.json"
    existing = []
    if archive_file.exists():
        try:
            existing = json.loads(archive_file.read_text())
        except Exception:
            pass
    archive_file.write_text(json.dumps(existing + archive, ensure_ascii=False, indent=2))
    _save(keep)
    return len(archive)


def _parse_recurrence(repeats: str) -> datetime.timedelta | None:
    """Parse a recurrence string into a timedelta."""
    if not repeats:
        return None
    repeats = repeats.lower().strip()
    if repeats in ("daily", "every day"):
        return datetime.timedelta(days=1)
    if repeats in ("weekly", "every week"):
        return datetime.timedelta(days=7)
    if repeats in ("biweekly", "every 2 weeks", "every two weeks", "fortnightly"):
        return datetime.timedelta(days=14)
    if repeats in ("monthly", "every month"):
        return datetime.timedelta(days=30)
    # "every N days/weeks"
    m = re.match(r'every\s+(\d+)\s+(day|week|month)s?', repeats)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "day":
            return datetime.timedelta(days=n)
        if unit == "week":
            return datetime.timedelta(days=n * 7)
        if unit == "month":
            return datetime.timedelta(days=n * 30)
    return None
