"""
skills/tasks/format.py — User-facing message renderers for task summaries.

Owns presentation only. Logic / data lives in local_tasks.py.

This module focuses on COMPACT summaries (counts + tap-to-copy hints) that
embed inside /today, /week, and other umbrella views. The rich numbered list
shown by /tasks itself still lives in local_tasks.format_tasks() — that one
is the full enumerated view tasks themselves act on.
"""

import datetime


def _count(tasks: "list[dict]", today: datetime.date) -> dict:
    """Bucket open tasks into overdue / today / week / undated."""
    today_iso = today.isoformat()
    week_end_iso = (today + datetime.timedelta(days=7)).isoformat()
    overdue = 0
    due_today = 0
    due_week = 0  # due in the next 7 days, excluding today (today is already counted)
    undated = 0
    for t in tasks:
        d = t.get("due_date") or ""
        if not d:
            undated += 1
        elif d < today_iso:
            overdue += 1
        elif d == today_iso:
            due_today += 1
        elif d <= week_end_iso:
            due_week += 1
    return {
        "overdue": overdue,
        "today": due_today,
        "week": due_week,
        "undated": undated,
    }


def render_task_counts(tasks: "list[dict]", period: str = "today") -> str:
    """One-line counts + tap-to-copy hint. Empty string if no tasks.

    period="today" → shows overdue + today + undated, hint targets the day.
    period="week"  → also shows "due this week", hint targets the week.
    """
    if not tasks:
        return ""
    today = datetime.date.today()
    c = _count(tasks, today)
    if not (c["overdue"] or c["today"] or c["week"] or c["undated"]):
        return ""

    bits: list[str] = []
    if c["overdue"]:
        bits.append(f"{c['overdue']} overdue")
    if c["today"]:
        bits.append(f"{c['today']} due today")
    if period == "week" and c["week"]:
        bits.append(f"{c['week']} this week")
    if c["undated"]:
        bits.append(f"{c['undated']} undated")
    if not bits:
        return ""

    header = "📋 " + " · ".join(bits)

    # Tap-to-copy hints — backticks render as monospace+copy in Telegram.
    # Order: most-urgent action first.
    hint_parts: list[str] = []
    if c["overdue"]:
        hint_parts.append("`/tasks overdue`")
    if c["today"]:
        hint_parts.append("`/tasks today`")
    if period == "week" and c["week"]:
        hint_parts.append("`/tasks week`")
    if not hint_parts:
        hint_parts.append("`/tasks`")
    hint = "↪ " + " · ".join(hint_parts)

    return f"{header}\n{hint}"


def render_completed(
    completed: "list[dict]",
    recurring_next: "list[dict] | None" = None,
) -> str:
    """Reply for /done — single or bulk task completion.

    Header with count, one bullet per task title (never a flat comma list),
    then any recurring tasks that were auto-recreated on their own lines.

    completed: list of completed task dicts (must have "title").
    recurring_next: list of {"title": str, "due_date": "YYYY-MM-DD"} dicts —
        new tasks created by recreate_recurring(); rendered after the bullets.
    """
    if not completed:
        return ""

    import datetime as _dt

    n = len(completed)
    if n == 1:
        lines = [f"✅ Done: {completed[0].get('title', '')}"]
    else:
        lines = [f"✅ Done ({n})"]
        for t in completed:
            lines.append(f"• {t.get('title', '')}")

    for nxt in recurring_next or []:
        title = nxt.get("title", "")
        due_iso = nxt.get("due_date", "")
        try:
            due = _dt.date.fromisoformat(due_iso).strftime("%-d %b")
        except (ValueError, TypeError):
            due = due_iso or "?"
        lines.append(f"🔄 Next: {title} — due {due}")

    return "\n".join(lines)
