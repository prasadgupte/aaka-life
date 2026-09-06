#!/usr/bin/env python3
"""
skills/task_manager.py — VPS-hosted task CRUD

Reads/writes to the same butler.db as the queue (QUEUE_DB env var).
Designed to run on both sensor (VPS) and executor (Mac) sides.
"""

import os
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
import aaka_config

_DEFAULT_DB = str(
    Path(os.environ.get("AAKA_CONFIG_DIR") or aaka_config.CONFIG_DIR)
    / "data" / "queue" / "butler.db"
)

_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass
    db_path = os.environ.get("QUEUE_DB", _DEFAULT_DB)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_task(
    title: str,
    namespace: str,
    notes: str = "",
    prompt: str = "",
    skill: str = "",
    project: str = "",
    due_date: str = "",
    source: str = "",
    sender: str = "",
) -> str:
    """Insert a new task. Returns the new task UUID."""
    task_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tasks
              (id, created_at, updated_at, namespace, title, notes, prompt,
               skill, project, due_date, status, source, sender)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (task_id, now, now, namespace, title, notes, prompt,
             skill, project, due_date, "todo", source, sender),
        )
    try:
        regenerate_tasks_md()
    except Exception:
        pass
    return task_id


def list_tasks(namespace: str, status: str = "todo") -> list[dict]:
    """Return tasks for a namespace filtered by status."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE namespace=? AND status=? ORDER BY created_at DESC",
            (namespace, status),
        ).fetchall()
    return [dict(r) for r in rows]


def complete_task(task_id: str) -> None:
    """Mark a task as done."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
            (_now(), task_id),
        )
    try:
        regenerate_tasks_md()
    except Exception:
        pass


def cancel_task(task_id: str) -> None:
    """Mark a task as cancelled."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?",
            (_now(), task_id),
        )


def get_task(task_id: str) -> dict | None:
    """Fetch a single task by ID. Returns None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    return dict(row) if row else None


def regenerate_tasks_md() -> Path:
    """Write vault/System/Tasks.md from all active tasks, grouped by namespace."""
    import sys
    sys.path.insert(0, str(BASE))
    from aaka_config import vault_path_for, default_actor

    vault = vault_path_for(default_actor())
    out = vault / "99-System" / "Tasks.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status='todo' ORDER BY namespace, due_date, created_at"
        ).fetchall()

    tasks = [dict(r) for r in rows]
    by_ns: dict[str, list] = {}
    for t in tasks:
        by_ns.setdefault(t["namespace"], []).append(t)

    lines = [
        "# Tasks",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]
    for ns, items in sorted(by_ns.items()):
        lines.append(f"## {ns}")
        for t in items:
            due = f" `due:{t['due_date']}`" if t.get("due_date") else ""
            proj = f" `#{t['project']}`" if t.get("project") else ""
            lines.append(f"- [ ] {t['title']}{due}{proj}  <!-- id:{t['id'][:8]} -->")
        lines.append("")

    out.write_text("\n".join(lines))
    return out


def bridge_to_calendar(task: dict) -> bool:
    """If task has due_date and notify=True in notes, create a GCal event.
    Returns True if a calendar event was created."""
    if not task.get("due_date"):
        return False
    notes = task.get("notes", "") or ""
    if "notify:true" not in notes.replace(" ", "").lower():
        return False

    import sys
    sys.path.insert(0, str(BASE))
    try:
        from skills.calendar import gog
        event = {
            "summary": task["title"],
            "start": {"date": task["due_date"]},
            "end": {"date": task["due_date"]},
            "description": notes,
        }
        gog.add_event(event)
        return True
    except Exception:
        return False
