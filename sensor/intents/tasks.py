"""sensor/intents/tasks.py — Local task view/manage handlers (no executor needed)."""
import re

import aaka_config

HANDLES = frozenset({"list_tasks", "snooze_task", "edit_task", "delete_task"})


def handle(intent: str, message: str, sender: str, channel_id: str, source: str) -> str:
    sender_member = aaka_config.member_by_sender(sender)
    if sender_member is None:
        return "🔒 Task access requires a recognised sender."

    if intent == "list_tasks":
        from sensor.router_sensor import _strip_me_flag
        _, me_flag = _strip_me_flag(message)
        from skills.tasks.local_tasks import list_open, format_tasks
        text = re.sub(r'^/tasks\s*', '', message, flags=re.I).strip()
        text = re.sub(r'(?i)\s*#me\b', '', text).strip()
        owner_filter = sender_member["id"] if me_flag else ""
        tag_filter = ""
        due_filter = ""
        label = ""

        kw = text.lower().split()[0] if text else ""

        if kw == "help":
            return (
                "📋 *Tasks*\n\n"
                "*Views*\n"
                "`/tasks` — this week + overdue\n"
                "`/tasks all` — all open tasks\n"
                "`/tasks inbox` — unplanned (no due date)\n"
                "`/tasks overdue` — overdue only\n"
                "`/tasks today` — due today\n"
                "`/tasks week` — due this week\n"
                "`/tasks #tag` — filter by tag\n"
                "`/tasks @name` — filter by owner\n\n"
                "*Actions*\n"
                "`t <task>` — add a task\n"
                "`/done N` — mark done  ·  `/done 1 3 5` — bulk\n"
                "`/snooze N 3d` — snooze  ·  `/snooze all 1d`\n"
                "`/edit N <change>` — update due/owner/title\n"
                "`/del N` — delete"
            )
        elif kw == "all":
            label = "all"
        elif text:
            tag_m = re.match(r'^#(\S+)', text)
            owner_m = re.match(r'^@(\S+)', text)
            if tag_m:
                tag_filter = f"#{tag_m.group(1)}"
                label = tag_filter
            elif owner_m:
                owner_filter = owner_m.group(1)
                label = f"@{owner_filter}"
            elif kw in ("overdue", "late"):
                due_filter = "overdue"
                label = "overdue"
            elif kw == "today":
                due_filter = "today"
                label = "today"
            elif kw in ("week", "this"):
                due_filter = "week"
                label = "this week"
            elif kw in ("nodate", "no", "undated", "inbox"):
                due_filter = "nodate"
                label = "no date"
        else:
            # Default: this week + overdue (due_filter="week" includes overdue tasks)
            due_filter = "week"
            label = "this week + overdue"

        tasks = list_open(owner=owner_filter, tag=tag_filter, due_filter=due_filter)
        return format_tasks(tasks, label=label)

    if intent == "snooze_task":
        text = re.sub(r'^/snooze\s*', '', message, flags=re.I).strip()
        bulk_m = re.match(r'^(all|overdue)\s+(?:(\d+)\s*([dw])|tomorrow)', text, re.I)
        if bulk_m:
            keyword = bulk_m.group(1).lower()
            if re.search(r'\btomorrow\b', text, re.I):
                days = 1
            else:
                n = int(bulk_m.group(2))
                days = n * 7 if bulk_m.group(3).lower() == 'w' else n
            from skills.tasks.local_tasks import snooze_all
            import datetime as _dt
            count = snooze_all(days, due_filter="overdue" if keyword == "overdue" else "all")
            if count == 0:
                return "No tasks to snooze."
            until = (_dt.date.today() + _dt.timedelta(days=days)).strftime("%-d %b")
            return f"💤 Snoozed {count} task{'s' if count != 1 else ''} until {until}"
        m = re.match(r'^(\d+)\s*(?:(\d+)\s*([dw])|tomorrow)?', text, re.I)
        if not m or not m.group(1):
            return "Usage: /snooze N [3d | 1w | tomorrow]\n       /snooze all 3d · /snooze overdue 1d"
        task_num = int(m.group(1))
        if re.search(r'\btomorrow\b', text, re.I):
            days = 1
        elif m.group(2) and m.group(3):
            n = int(m.group(2))
            days = n * 7 if m.group(3).lower() == 'w' else n
        else:
            days = 1
        from skills.tasks.local_tasks import snooze_task_by_num, title_for_num
        try:
            name = title_for_num(task_num)
            snooze_task_by_num(task_num, days)
            import datetime as _dt
            until = (_dt.date.today() + _dt.timedelta(days=days)).strftime("%-d %b")
            return f"💤 Snoozed: {name}\n↪ Resurfaces {until}"
        except Exception as exc:
            return f"⚠️ {exc}"

    if intent == "edit_task":
        text = re.sub(r'^/edit\s*', '', message, flags=re.I).strip()
        m = re.match(r'^(\d+)\s+(.*)', text)
        if not m:
            return "Usage: /edit N <change>\nExamples: /edit 2 friday · /edit 2 @bob · /edit 2 !high · /edit 2 new title"
        task_num = int(m.group(1))
        change = m.group(2).strip()
        from skills.tasks.local_tasks import update_task_by_num, title_for_num
        from sensor.router_sensor import _parse_edit_date
        updates = {}
        change_desc = ""
        if re.match(r'^@\S+$', change):
            owner = change[1:].lower()
            member = aaka_config.member_by_name(owner)
            if not member:
                return f"⚠️ Unknown member '{owner}'. Check /members."
            updates["owner"] = member["id"]
            change_desc = f"owner → {member['id']}"
        elif re.match(r'^!(?:high|med|low|urgent|normal)$', change, re.I):
            prio = change[1:].lower()
            updates["priority"] = prio
            change_desc = f"priority → {prio}"
        elif re.match(r'^[0-9]', change) or change.lower() in (
            "today", "tomorrow", "next week", "none", "no date", "clear"
        ) or re.match(r'^[a-z]+day$', change, re.I) or re.match(r'^\d+[dw]$', change, re.I):
            new_date = _parse_edit_date(change)
            updates["due_date"] = new_date
            change_desc = f"due → {new_date or 'removed'}"
        else:
            updates["title"] = change
            change_desc = f"title → {change}"
        try:
            name = title_for_num(task_num)
            update_task_by_num(task_num, updates)
            return f"✏️ Updated #{task_num} ({name}): {change_desc}"
        except Exception as exc:
            return f"⚠️ {exc}"

    if intent == "delete_task":
        text = re.sub(r'^/(del|delete)\s*', '', message, flags=re.I).strip()
        num_match = re.match(r'^(\d+)\s*$', text)
        if not num_match:
            return "Usage: /del N\nExample: /del 3"
        task_num = int(num_match.group(1))
        from skills.tasks.local_tasks import delete_task_by_num, title_for_num
        try:
            name = title_for_num(task_num)
            delete_task_by_num(task_num)
            return f"🗑️ Deleted: {name}"
        except Exception as exc:
            return f"⚠️ {exc}"

    return "❓ Unknown task intent."
