"""
taskboard/api.py — Task CRUD API for the local taskboard UI.

No agent auth — localhost-only. Reads/writes local_tasks directly.
All tasks visible (cross-agent), not scoped to a single agent.
"""
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Optional

BASE = Path(os.environ.get("AAKA_BASE", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(BASE))

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import aaka_config
from skills.tasks.local_tasks import (
    _load,
    _save,
    _now,
    add_task,
    complete_task_by_id,
    delete_task,
    update_task_by_id,
    snooze_task,
)

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.date.today().isoformat()


def _is_visible(t: dict) -> bool:
    if t.get("status") not in ("open", None):
        return False
    snooze = t.get("snooze_until")
    if snooze and snooze > _today():
        return False
    return True


def _task_out(t: dict) -> dict:
    priority = t.get("priority", "normal")
    return {
        "id":           t["id"],
        "title":        t.get("title", ""),
        "owner":        t.get("owner") or "",
        "due_date":     t.get("due_date") or None,
        "priority":     priority,
        "snooze_until": t.get("snooze_until") or None,
        "tags":         [tag for tag in (t.get("tags") or []) if not tag.startswith("#agent:")],
        "description":  t.get("description") or "",
        "recurring":    t.get("recurring", False),
        "repeats":      t.get("repeats") or "",
        "project":      t.get("project") or None,
        "agent":        t.get("agent") or None,
        "skill":        t.get("skill") or None,
        "status":       t.get("status", "open"),
        "created_at":   t.get("created_at") or None,
        "updated_at":   t.get("updated_at") or None,
        "done_at":      t.get("done_at") or None,
    }


def _due_bucket(due_str: str) -> str:
    if not due_str:
        return "nodate"
    try:
        due = datetime.date.fromisoformat(due_str)
    except ValueError:
        return "nodate"
    today = datetime.date.today()
    delta = (due - today).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta <= 7:
        return "week"
    return "later"


# ── GET /api/tasks ─────────────────────────────────────────────────────────────

@router.get("/api/tasks")
def list_tasks(
    status:  str = "open",
    owner:   str = "",
    agent:   str = "",
    project: str = "",
    due:     str = "",
    tag:     list[str] = Query(default=[]),
    q:       str = "",
):
    """List tasks with optional filters.

    status: open (default) | done | deleted | all | snoozed
    due:    overdue | today | week | nodate | later | all (default all visible)
    """
    tasks = _load()

    if status == "open":
        tasks = [t for t in tasks if _is_visible(t)]
    elif status == "snoozed":
        today = _today()
        tasks = [t for t in tasks if t.get("status") == "open" and (t.get("snooze_until") or "") > today]
    elif status != "all":
        tasks = [t for t in tasks if t.get("status") == status]

    if owner:
        tasks = [t for t in tasks if t.get("owner") == owner]
    if agent:
        tasks = [t for t in tasks if t.get("agent") == agent]
    if project:
        tasks = [t for t in tasks if t.get("project") == project]
    if tag:
        tag_set = set(tag)
        tasks = [t for t in tasks if tag_set.intersection(t.get("tags") or [])]
    if q:
        ql = q.lower()
        tasks = [t for t in tasks if ql in t.get("title", "").lower()]
    if due and due != "all":
        tasks = [t for t in tasks if _due_bucket(t.get("due_date") or "") == due]

    # Sort: overdue first, then by due asc, then no-date by created desc
    def _sort_key(t):
        d = t.get("due_date") or ""
        has_due = bool(d)
        try:
            due_d = datetime.date.fromisoformat(d) if d else datetime.date(9999, 1, 1)
        except ValueError:
            due_d = datetime.date(9999, 1, 1)
        prio_order = {"high": 0, "medium": 1, "normal": 2}.get(t.get("priority", "normal"), 2)
        return (not has_due, due_d, prio_order, t.get("created_at") or "")

    tasks.sort(key=_sort_key)

    return {
        "tasks":  [_task_out(t) for t in tasks],
        "count":  len(tasks),
    }


# ── GET /api/tasks/{id} ────────────────────────────────────────────────────────

@router.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    for t in _load():
        if t["id"] == task_id:
            return _task_out(t)
    raise HTTPException(status_code=404, detail="Task not found")


# ── POST /api/tasks ────────────────────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    title:       str
    owner:       str = ""
    due_date:    Optional[str] = None
    priority:    str = "normal"
    tags:        list[str] = []
    description: str = ""
    project:     Optional[str] = None
    agent:       Optional[str] = None


@router.post("/api/tasks", status_code=201)
def create_task(body: CreateTaskRequest):
    payload = {
        "title":       body.title,
        "owner":       body.owner,
        "due_date":    body.due_date or "",
        "urgent":      body.priority == "high",
        "starred":     body.priority == "medium",
        "tags":        body.tags,
        "description": body.description,
        "project":     body.project,
        "agent":       body.agent or "taskboard",
    }
    task = add_task(payload)
    return _task_out(task)


# ── PATCH /api/tasks/{id} ──────────────────────────────────────────────────────

class UpdateTaskRequest(BaseModel):
    title:        Optional[str] = None
    owner:        Optional[str] = None
    due_date:     Optional[str] = None
    priority:     Optional[str] = None   # high | medium | normal
    tags:         Optional[list[str]] = None
    description:  Optional[str] = None
    recurring:    Optional[bool] = None
    repeats:      Optional[str] = None
    project:      Optional[str] = None
    status:       Optional[str] = None   # done | deleted | open
    snooze_until: Optional[str] = None
    snooze_days:  Optional[int] = None   # convenience: snooze N days from today


@router.patch("/api/tasks/{task_id}")
def update_task(task_id: str, body: UpdateTaskRequest):
    # Verify task exists
    tasks = _load()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Handle status shortcuts
    status = body.status
    if status == "done":
        try:
            complete_task_by_id(task_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if not any(v is not None for v in [body.title, body.owner, body.due_date,
                                            body.priority, body.tags, body.description,
                                            body.recurring, body.repeats, body.project,
                                            body.snooze_until, body.snooze_days]):
            return {"status": "done"}

    elif status == "deleted":
        try:
            delete_task(task_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"status": "deleted"}

    # Handle snooze
    if body.snooze_days is not None:
        try:
            snooze_task(task_id, body.snooze_days)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # Build field updates
    updates = {}
    if body.title is not None:
        updates["title"] = body.title
    if body.owner is not None:
        updates["owner"] = body.owner
    if body.due_date is not None:
        updates["due_date"] = body.due_date
    if body.priority is not None:
        updates["priority"] = body.priority
    if body.tags is not None:
        updates["tags"] = body.tags
    if body.description is not None:
        updates["description"] = body.description
    if body.recurring is not None:
        updates["recurring"] = body.recurring
    if body.repeats is not None:
        updates["repeats"] = body.repeats
    if body.project is not None:
        updates["project"] = body.project
    if body.snooze_until is not None:
        updates["snooze_until"] = body.snooze_until

    if updates:
        try:
            result = update_task_by_id(task_id, updates)
            return _task_out(result["task"])
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # Re-fetch after snooze-only update
    for t in _load():
        if t["id"] == task_id:
            return _task_out(t)
    return {}


# ── DELETE /api/tasks/{id} ─────────────────────────────────────────────────────

@router.delete("/api/tasks/{task_id}", status_code=204)
def delete_task_endpoint(task_id: str):
    try:
        delete_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── POST /api/tasks/bulk-action ───────────────────────────────────────────────

class BulkActionRequest(BaseModel):
    ids:          list[str]
    action:       str              # "done" | "delete" | "snooze"
    snooze_days:  Optional[int]  = None
    snooze_until: Optional[str]  = None


@router.post("/api/tasks/bulk-action")
def bulk_action(body: BulkActionRequest):
    """Apply an action to a specific list of task IDs.

    action: done | delete | snooze
    Returns {affected: int, task_ids: [str]}.
    """
    if not body.ids:
        raise HTTPException(status_code=400, detail="ids list is empty")
    if body.action not in ("done", "delete", "snooze"):
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action!r}")

    id_set = set(body.ids)
    tasks  = _load()
    now    = _now()
    affected_ids = []

    for t in tasks:
        if t["id"] not in id_set:
            continue
        if body.action == "done":
            if t.get("status") in ("open", None):
                t["status"]     = "done"
                t["done_at"]    = now
                t["updated_at"] = now
                affected_ids.append(t["id"])
        elif body.action == "delete":
            if t.get("status") != "deleted":
                t["status"]     = "deleted"
                t["updated_at"] = now
                affected_ids.append(t["id"])
        elif body.action == "snooze":
            if t.get("status") in ("open", None):
                if body.snooze_days is not None:
                    until = (datetime.date.today() + datetime.timedelta(days=body.snooze_days)).isoformat()
                elif body.snooze_until:
                    until = body.snooze_until
                else:
                    continue
                t["snooze_until"] = until
                t["updated_at"]   = now
                affected_ids.append(t["id"])

    if affected_ids:
        _save(tasks)

    return {"affected": len(affected_ids), "task_ids": affected_ids}


# ── GET /api/meta ──────────────────────────────────────────────────────────────

@router.get("/api/meta")
def get_meta():
    """Return distinct filter values + open-task counts per bucket."""
    tasks = _load()
    open_tasks = [t for t in tasks if _is_visible(t)]
    snoozed = [t for t in tasks if t.get("status") == "open" and not _is_visible(t)]

    owners  = sorted({t.get("owner") for t in open_tasks if t.get("owner")})
    agents  = sorted({t.get("agent") for t in open_tasks if t.get("agent")})
    projects = sorted({t.get("project") for t in open_tasks if t.get("project")})

    # Tag counts — only open tasks, sorted by count desc
    tag_counts: dict[str, int] = {}
    for t in open_tasks:
        for tag in (t.get("tags") or []):
            if not tag.startswith("#agent:"):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
    # Return as list of {name, count} sorted by count desc, then name
    tags = sorted(
        [{"name": k, "count": v} for k, v in tag_counts.items()],
        key=lambda x: (-x["count"], x["name"]),
    )

    # Count per due bucket
    buckets: dict[str, int] = {"overdue": 0, "today": 0, "week": 0, "later": 0, "nodate": 0}
    for t in open_tasks:
        buckets[_due_bucket(t.get("due_date") or "")] += 1

    # Known members from config (for owner autocomplete)
    try:
        member_names = [m["id"] for m in aaka_config.members()]
    except Exception:
        member_names = []

    return {
        "owners":       owners,
        "agents":       agents,
        "projects":     projects,
        "tags":         tags,          # [{name, count}] sorted by count desc
        "members":      member_names,
        "counts": {
            "open":    len(open_tasks),
            "snoozed": len(snoozed),
            "done":    sum(1 for t in tasks if t.get("status") == "done"),
            **buckets,
        },
    }
