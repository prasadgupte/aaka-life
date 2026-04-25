"""
skills/lists/format.py — User-facing message renderers for buy lists.

All Telegram/WhatsApp strings produced by the buy-list intent live here.
Logic functions in list_manager.py return structured data; this module
owns presentation. Edit here to tweak wording, emojis, or layout.
"""


def render_lists_index(entries: "list[tuple[str, int, bool]]") -> str:
    """entries: [(name, unchecked_count, is_shared)]."""
    if not entries:
        return "No lists yet.\n↪ b <name> <item>"
    lines = ["📋 Lists:"]
    for name, n, shared in entries:
        tag = " 👥" if shared else ""
        lines.append(f"  *{name}* ({n}){tag}")
    return "\n".join(lines)


def render_list_view(
    list_name: str,
    unchecked: "list[str]",
    checked_count: int,
    shared: bool,
    *,
    added_count: int = 0,
) -> str:
    """Render numbered unchecked items. If added_count > 0, the LAST
    added_count items (newly appended) are marked with a trailing '*'
    and an explanatory hint line is appended."""
    label = f"📋 *{list_name}*" + (" 👥" if shared else "")
    if not unchecked and not checked_count:
        return f"📋 *{list_name}* — empty\n↪ b {list_name} <item>"

    total = len(unchecked)
    if added_count > 0:
        new_word = "new" if added_count == 1 else "new"
        header = f"{label} ({total}) — {added_count} {new_word} *"
    else:
        header = label

    parts = [header]
    first_new = total - added_count if added_count > 0 else total
    for i, item in enumerate(unchecked, 1):
        marker = " *" if added_count > 0 and i > first_new else ""
        parts.append(f"  {i}. {item}{marker}")

    if checked_count:
        parts.append(f"  ~~({checked_count} done)~~")

    if added_count > 0:
        parts.append("↪ * = added now · numbers to check off, e.g. 1 3")
    else:
        parts.append("↪ numbers to check off, e.g. 1 3")
    return "\n".join(parts)


def _days_ago(date_str: "str | None") -> str:
    """Format a YYYY-MM-DD as '-Nd' (or 'today', 'yesterday'). Empty if no date."""
    if not date_str:
        return ""
    import datetime
    try:
        dt = datetime.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return ""
    delta = (datetime.date.today() - dt).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "yesterday"
    return f"-{delta}d"


def render_ideas_view(data: dict) -> str:
    """Render the ideas pool for a list.

    data = {"list_name", "ideas": [{"item", "last_bought", "source"}], "shared"}
    """
    name = data["list_name"]
    items = data["ideas"]
    label = f"💡 *{name}* — ideas"
    if not items:
        return (
            f"{label}\n"
            "No history or seed entries yet for this list.\n"
            f"↪ shop here a few times, or drop ideas in `_seed/{name}.md`"
        )
    parts = [f"{label} ({len(items)})"]
    for i, entry in enumerate(items, 1):
        tag = _days_ago(entry["last_bought"]) if entry["source"] == "history" else "seed"
        suffix = f"  (✓ {tag})" if entry["source"] == "history" else "  (seed)"
        parts.append(f"  {i}. {entry['item']}{suffix}")
    parts.append("↪ reply  `add 1 3 5`  or just  `1 3 5`  to add to list")
    return "\n".join(parts)


def render_add_confirm(list_name: str, added: "list[str]", total: int) -> str:
    """Short confirmation only (no full list). Used by callers that don't
    want to show the whole list after adding."""
    parts = [f"➕ {len(added)} to 📋 *{list_name}* ({total})"]
    for item in added:
        parts.append(f"  {item}")
    return "\n".join(parts)
