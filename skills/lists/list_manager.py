"""
skills/lists/list_manager.py — Sensor-side list management.

Lists are personal (per-member) by default.
Use `#share` to make a list shared, `#private` to take it back.

Storage layout:
  data/lists/_shared/grocery.md     ← shared lists
  data/lists/kai/todo.md            ← personal lists (per namespace)
"""

import datetime
import os
import re
import shutil
from pathlib import Path


def _lists_dir() -> Path:
    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
    d = config_dir / "data" / "lists"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    safe = re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')
    return safe or "list"


_META_RE = re.compile(r'\s*<!--\s*(.+?)\s*-->\s*$')

ARCHIVE_HEADING = "## archive"

# Seed file category → list names. Edit when new markets or categories appear.
SEED_CATEGORY_TO_MARKETS: "dict[str, set[str]]" = {
    "food": {"rewe", "kaufland", "lidl"},
    "drugs": {"rossmann"},
    "fresh": {"market"},
}


def _split_archive(text: str) -> "tuple[str, str]":
    """Split file body into (active_text, archive_text).

    archive_text includes the '## archive' heading; empty string if no archive
    section exists. active_text always ends with a single trailing newline
    (or is empty)."""
    if not text:
        return "", ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower() == ARCHIVE_HEADING:
            active = "\n".join(lines[:i]).rstrip()
            if active:
                active += "\n"
            archive = "\n".join(lines[i:])
            return active, archive
    return text, ""


def _write_parts(path: Path, active: str, archive: str) -> None:
    """Write active + archive back to file with a blank line separator."""
    out = active.rstrip() + "\n"
    if archive.strip():
        out += "\n" + archive.rstrip() + "\n"
    path.write_text(out)


def _strip_meta(text: str) -> str:
    """Remove <!-- ... --> metadata from item text."""
    return _META_RE.sub('', text)


def _parse_meta(text: str) -> dict:
    """Extract metadata from <!-- +user YYYY-MM-DD ✓user YYYY-MM-DD -->."""
    m = _META_RE.search(text)
    if not m:
        return {}
    raw = m.group(1)
    meta: dict = {}
    add_m = re.search(r'\+(\S+)\s+(\d{4}-\d{2}-\d{2})', raw)
    if add_m:
        meta["added_by"] = add_m.group(1)
        meta["added_at"] = add_m.group(2)
    chk_m = re.search(r'✓(\S+)\s+(\d{4}-\d{2}-\d{2})', raw)
    if chk_m:
        meta["checked_by"] = chk_m.group(1)
        meta["checked_at"] = chk_m.group(2)
    return meta


def _rel_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to relative or short date."""
    try:
        dt = datetime.date.fromisoformat(date_str)
        delta = (datetime.date.today() - dt).days
        if delta == 0:
            return "today"
        if delta == 1:
            return "yesterday"
        if delta < 7:
            return f"{delta}d ago"
        return dt.strftime("%d-%b")
    except (ValueError, TypeError):
        return date_str


def _resolve_list(name: str, namespace: str) -> Path:
    """Find a list: shared first, then personal, then legacy root. Returns the path (may not exist yet)."""
    safe = _safe_name(name)
    shared = _lists_dir() / "_shared" / f"{safe}.md"
    if shared.exists():
        return shared
    personal = _lists_dir() / namespace / f"{safe}.md"
    if personal.exists():
        return personal
    # Legacy: file at root level (pre-namespace migration)
    legacy = _lists_dir() / f"{safe}.md"
    if legacy.exists():
        return legacy
    # Doesn't exist yet — default to personal
    return personal


def _list_path(name: str, namespace: str = "") -> Path:
    """Resolve list path. Used by router for file_sync dest calculation."""
    if namespace:
        return _resolve_list(name, namespace)
    # Fallback for legacy callers without namespace
    return _lists_dir() / f"{_safe_name(name)}.md"


def is_shared(name: str) -> bool:
    """Check if a list is in the shared directory."""
    return (_lists_dir() / "_shared" / f"{_safe_name(name)}.md").exists()


def share_list(name: str, namespace: str) -> str:
    """Move a personal list to shared."""
    safe = _safe_name(name)
    shared_dir = _lists_dir() / "_shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    shared_path = shared_dir / f"{safe}.md"
    personal_path = _lists_dir() / namespace / f"{safe}.md"

    if shared_path.exists():
        return f"📋 *{name}* is already shared."
    if not personal_path.exists():
        return f"No list *{name}* to share."
    shutil.move(str(personal_path), str(shared_path))
    return f"📋 *{name}* is now shared with the family 👥"


def unshare_list(name: str, namespace: str) -> str:
    """Move a shared list to personal."""
    safe = _safe_name(name)
    shared_path = _lists_dir() / "_shared" / f"{safe}.md"
    personal_dir = _lists_dir() / namespace
    personal_dir.mkdir(parents=True, exist_ok=True)
    personal_path = personal_dir / f"{safe}.md"

    if not shared_path.exists():
        return f"📋 *{name}* is not a shared list."
    if personal_path.exists():
        return f"📋 You already have a personal *{name}* list. Rename it first."
    shutil.move(str(shared_path), str(personal_path))
    return f"📋 *{name}* is now your private list."


def add_items(list_name: str, items: "list[str]", namespace: str = "") -> dict:
    """Append one or more unchecked items.

    Returns: {"list_name", "added": [str], "total": int, "shared": bool}.
    Presentation lives in skills/lists/format.py.
    """
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    is_new = not path.exists()
    if is_new:
        path.parent.mkdir(parents=True, exist_ok=True)
    clean = [i.strip() for i in items if i.strip()]
    today = datetime.date.today().isoformat()
    new_lines = "".join(f"- [ ] {item} <!-- +{namespace or '?'} {today} -->\n" for item in clean)
    if is_new:
        path.write_text(f"# {list_name}\n{new_lines}")
    else:
        active, archive = _split_archive(path.read_text())
        active = (active.rstrip() + "\n") if active.strip() else f"# {list_name}\n"
        active += new_lines
        _write_parts(path, active, archive)
    shared = path.parent.name == "_shared"
    return {
        "list_name": list_name,
        "added": clean,
        "total": _count_unchecked(path),
        "shared": shared,
    }


def list_state(list_name: str, namespace: str = "") -> "dict | None":
    """Return current unchecked items + checked count for a list, or None if missing.

    {"list_name", "unchecked": [str], "checked_count": int, "shared": bool}
    """
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    if not path.exists():
        return None
    shared = path.parent.name == "_shared"
    active, _ = _split_archive(path.read_text())
    lines = active.strip().split("\n")
    unchecked = [_strip_meta(l.replace("- [ ] ", "")) for l in lines if l.startswith("- [ ]")]
    checked_count = sum(1 for l in lines if l.startswith("- [x]"))
    return {
        "list_name": list_name,
        "unchecked": unchecked,
        "checked_count": checked_count,
        "shared": shared,
    }


def show(list_name: str, namespace: str = "") -> str:
    """Return the list with numbered unchecked items for easy check-off."""
    from skills.lists.format import render_list_view
    state = list_state(list_name, namespace)
    if state is None:
        return f"No list *{list_name}* yet.\n↪ b {list_name} <item>"
    return render_list_view(
        state["list_name"], state["unchecked"], state["checked_count"], state["shared"],
    )


def check_off(list_name: str, hint: str, namespace: str = "") -> str:
    """Mark items done by number(s) or keyword match."""
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    if not path.exists():
        return f"No list *{list_name}*."
    active, archive = _split_archive(path.read_text())
    lines = active.split("\n")
    unchecked_indices = [i for i, l in enumerate(lines) if l.startswith("- [ ]")]

    today = datetime.date.today().isoformat()

    def _mark_done(line: str) -> tuple[str, str]:
        """Mark a line as checked, add ✓ metadata, return (new_line, display_text)."""
        new_line = line.replace("- [ ]", "- [x]", 1)
        # Append ✓ metadata into existing comment or add new one
        m = _META_RE.search(new_line)
        if m:
            meta_content = m.group(1) + f" ✓{namespace} {today}"
            new_line = _META_RE.sub(f" <!-- {meta_content} -->", new_line)
        else:
            new_line = new_line.rstrip() + f" <!-- ✓{namespace} {today} -->"
        return new_line, _strip_meta(line.replace("- [ ] ", ""))

    # Try number-based check-off: "1 3" or "1"
    nums = re.findall(r'\b(\d+)\b', hint)
    if nums and all(n.isdigit() for n in hint.split()):
        checked_items = []
        for n in nums:
            idx = int(n) - 1
            if 0 <= idx < len(unchecked_indices):
                line_idx = unchecked_indices[idx]
                lines[line_idx], item_text = _mark_done(lines[line_idx])
                checked_items.append(item_text)
        if checked_items:
            _write_parts(path, "\n".join(lines), archive)
            remaining = _count_unchecked(path)
            done_str = ", ".join(f"~~{t}~~" for t in checked_items)
            return f"✓ {done_str} — 📋 *{list_name}* ({remaining} left)"
        return f"No items matching those numbers in *{list_name}*."

    # Keyword-based check-off
    hint_lower = hint.lower()
    for i, line in enumerate(lines):
        if line.startswith("- [ ]") and hint_lower in _strip_meta(line).lower():
            lines[i], item_text = _mark_done(line)
            _write_parts(path, "\n".join(lines), archive)
            remaining = _count_unchecked(path)
            return f"✓ ~~{item_text}~~ — 📋 *{list_name}* ({remaining} left)"
    return f"No unchecked item matching '{hint}' in *{list_name}*."


def show_all(list_name: str, namespace: str = "") -> str:
    """Show all items including done ones."""
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    if not path.exists():
        return f"No list *{list_name}* yet.\n↪ b {list_name} <item>"
    shared = path.parent.name == "_shared"
    label = f"📋 *{list_name}*" + (" 👥" if shared else "") + " (all)"
    active, _ = _split_archive(path.read_text())
    lines = active.strip().split("\n")
    unchecked = [(False, _strip_meta(l.replace("- [ ] ", ""))) for l in lines if l.startswith("- [ ]")]
    checked = [(True, _strip_meta(l.replace("- [x] ", ""))) for l in lines if l.startswith("- [x]")]
    all_items = unchecked + checked
    if not all_items:
        return f"📋 *{list_name}* — empty\n↪ b {list_name} <item>"
    parts = [label]
    for i, (done, item) in enumerate(all_items, 1):
        text = f"~~{item}~~" if done else item
        parts.append(f"  {i}. {text}")
    return "\n".join(parts)


def audit(list_name: str, namespace: str = "") -> str:
    """Show all items with who added/checked and when."""
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    if not path.exists():
        return f"No list *{list_name}* yet."
    shared = path.parent.name == "_shared"
    label = f"📋 *{list_name}*" + (" 👥" if shared else "") + " audit"
    active, _ = _split_archive(path.read_text())
    lines = active.strip().split("\n")
    item_lines = [l for l in lines if l.startswith("- [ ]") or l.startswith("- [x]")]
    if not item_lines:
        return f"📋 *{list_name}* — empty"
    parts = [label]
    for i, line in enumerate(item_lines, 1):
        done = line.startswith("- [x]")
        raw_text = line.replace("- [x] ", "", 1) if done else line.replace("- [ ] ", "", 1)
        item_text = _strip_meta(raw_text)
        meta = _parse_meta(raw_text)
        # Build metadata string
        info = []
        if meta.get("added_by"):
            info.append(f"➕ {meta['added_by']}, {_rel_date(meta['added_at'])}")
        if meta.get("checked_by"):
            info.append(f"✓ {meta['checked_by']}, {_rel_date(meta['checked_at'])}")
        display = f"~~{item_text}~~" if done else item_text
        if info:
            display += f" ({' · '.join(info)})"
        parts.append(f"  {i}. {display}")
    return "\n".join(parts)


def done_all(list_name: str, namespace: str = "") -> str:
    """Mark all unchecked items as done."""
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    if not path.exists():
        return f"No list *{list_name}*."
    active, archive = _split_archive(path.read_text())
    lines = active.split("\n")
    count = sum(1 for l in lines if l.startswith("- [ ]"))
    if not count:
        return f"No open items in 📋 *{list_name}*."
    today = datetime.date.today().isoformat()
    for i, line in enumerate(lines):
        if line.startswith("- [ ]"):
            new_line = line.replace("- [ ]", "- [x]", 1)
            m = _META_RE.search(new_line)
            if m:
                meta_content = m.group(1) + f" ✓{namespace} {today}"
                new_line = _META_RE.sub(f" <!-- {meta_content} -->", new_line)
            else:
                new_line = new_line.rstrip() + f" <!-- ✓{namespace} {today} -->"
            lines[i] = new_line
    _write_parts(path, "\n".join(lines), archive)
    return f"✓ All {count} items done in 📋 *{list_name}*"


def clear_done(list_name: str, namespace: str = "") -> str:
    """Move all checked-off items to the ## archive section (preserves history
    for the `ideas` view)."""
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    if not path.exists():
        return f"No list *{list_name}*."
    active, archive = _split_archive(path.read_text())
    active_lines = active.split("\n")
    done_lines = [l for l in active_lines if l.startswith("- [x]")]
    if not done_lines:
        return f"No done items to clear in *{list_name}*."
    kept_lines = [l for l in active_lines if not l.startswith("- [x]")]
    if archive.strip():
        # Existing archive — append below existing archived lines
        new_archive = archive.rstrip() + "\n" + "\n".join(done_lines)
    else:
        new_archive = ARCHIVE_HEADING + "\n" + "\n".join(done_lines)
    _write_parts(path, "\n".join(kept_lines), new_archive)
    return f"🧹 Archived {len(done_lines)} done from 📋 *{list_name}*"


def list_all(namespace: str = "", include_shared: bool = True) -> str:
    """Show all lists visible to this member: shared + personal.

    Pass include_shared=False to omit shared lists (e.g. for restricted members).
    """
    d = _lists_dir()
    shared_dir = d / "_shared"
    personal_dir = d / namespace if namespace else None

    entries: list[tuple[str, int, bool]] = []  # (name, count, is_shared)

    if include_shared and shared_dir.exists():
        for f in sorted(shared_dir.glob("*.md")):
            entries.append((f.stem, _count_unchecked(f), True))
    if personal_dir and personal_dir.exists():
        for f in sorted(personal_dir.glob("*.md")):
            entries.append((f.stem, _count_unchecked(f), False))
    # Legacy: root-level lists (pre-namespace migration)
    for f in sorted(d.glob("*.md")):
        if f.stem not in {e[0] for e in entries}:
            entries.append((f.stem, _count_unchecked(f), False))

    from skills.lists.format import render_lists_index
    return render_lists_index(entries)


def preview_done_all(list_name: str, namespace: str = "") -> str:
    """Preview for #done confirmation."""
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    if not path.exists():
        return f"No list *{list_name}*."
    count = _count_unchecked(path)
    if not count:
        return f"No open items in 📋 *{list_name}*."
    return f"Mark all {count} items done in 📋 *{list_name}*?\n↪ y to confirm"


def preview_clear(list_name: str, namespace: str = "") -> str:
    """Preview for #clear confirmation."""
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    if not path.exists():
        return f"No list *{list_name}*."
    active, _ = _split_archive(path.read_text())
    lines = active.split("\n")
    done_count = sum(1 for l in lines if l.startswith("- [x]"))
    if not done_count:
        return f"No done items to clear in 📋 *{list_name}*."
    return f"Archive {done_count} done items in 📋 *{list_name}*?\n↪ y to confirm"


def _seed_dir() -> Path:
    return _lists_dir() / "_seed"


# WhatsApp paste prefix: "[12:34, 1/23/2026] Sender Name: " (leading [ sometimes lost on copy)
_WHATSAPP_PREFIX_RE = re.compile(r'^\[?\d{1,2}:\d{2},\s*\d{1,2}/\d{1,2}/\d{2,4}\][^:]*:\s*')
# Leading bullets / unicode whitespace. ⁠ is the WORD JOINER seen in the seed.
_BULLET_RE = re.compile(r'^[-•⁠*\s⁠ ]+')


def _clean_seed_line(raw: str) -> str:
    """Normalize a single seed line. Returns '' if line should be skipped."""
    line = _WHATSAPP_PREFIX_RE.sub('', raw)
    line = _BULLET_RE.sub('', line)
    line = line.strip()
    if not line:
        return ''
    # Sub-heading like "Rewe:", "Sona:", "Market:" — not an item.
    if line.endswith(':'):
        return ''
    return line


def _read_seed_file(path: Path) -> "list[tuple[str, str]]":
    """Parse a seed file. Returns list of (category, item) tuples.

    For per-market files (no '** category' headers), category is ''.
    """
    if not path.exists():
        return []
    out: "list[tuple[str, str]]" = []
    category = ''
    for raw in path.read_text().splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith('**'):
            # Section header e.g. "** Food"
            category = stripped.lstrip('*').strip().lower()
            continue
        if stripped.startswith('#'):
            continue  # comment
        item = _clean_seed_line(raw)
        if item:
            out.append((category, item))
    return out


def _seed_items_for(list_name: str) -> "list[str]":
    """Collect seed items applicable to `list_name`, deduped (case-insensitive).

    Sources: _seed/ideas.md (filtered by category mapping) + _seed/<name>.md."""
    name = _safe_name(list_name)
    seen: dict = {}  # lowercased -> display form (first occurrence wins)

    ideas_path = _seed_dir() / "ideas.md"
    for category, item in _read_seed_file(ideas_path):
        markets = SEED_CATEGORY_TO_MARKETS.get(category, set())
        if name in markets:
            seen.setdefault(item.lower(), item)

    per_list = _seed_dir() / f"{name}.md"
    for _, item in _read_seed_file(per_list):
        seen.setdefault(item.lower(), item)

    return list(seen.values())


def ideas(list_name: str, namespace: str = "") -> dict:
    """Build the "ideas" pool for a list: items we typically buy here.

    Sources:
      - Historical ✓ entries (active + archive sections of the list file)
      - Seed file entries applicable to this list

    Filters out items currently on the active list (would be a duplicate add).
    Sort: by last_bought desc; seed-only items (no date) alphabetical at end.

    Returns: {"list_name", "ideas": [{"item", "last_bought", "source"}], "shared"}
    """
    path = _resolve_list(list_name, namespace) if namespace else _list_path(list_name)
    shared = path.exists() and path.parent.name == "_shared"

    # Step 1: collect history (max ✓date per item, across active + archive).
    history: dict = {}  # lowercased -> (display, last_bought_iso)
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.startswith("- [x]"):
                continue
            raw = line.replace("- [x] ", "", 1)
            item_text = _strip_meta(raw).strip()
            if not item_text:
                continue
            meta = _parse_meta(raw)
            date = meta.get("checked_at")
            key = item_text.lower()
            prev = history.get(key)
            if prev is None or (date and (prev[1] is None or date > prev[1])):
                history[key] = (item_text, date)

    # Step 2: collect seed items.
    seed = _seed_items_for(list_name)

    # Step 3: items currently open on the active list — exclude from ideas.
    state = list_state(list_name, namespace)
    on_list = {item.strip().lower() for item in (state["unchecked"] if state else [])}

    # Step 4: merge. History wins display + date. Seed-only items have no date.
    merged: dict = {}  # key -> {item, last_bought, source}
    for key, (display, date) in history.items():
        if key in on_list:
            continue
        merged[key] = {"item": display, "last_bought": date, "source": "history"}
    for item in seed:
        key = item.lower()
        if key in on_list or key in merged:
            continue
        merged[key] = {"item": item, "last_bought": None, "source": "seed"}

    # Step 5: sort. Dated items first by date desc; seed-only alphabetical at end.
    dated = [e for e in merged.values() if e["last_bought"]]
    undated = [e for e in merged.values() if not e["last_bought"]]
    dated.sort(key=lambda e: e["last_bought"], reverse=True)
    undated.sort(key=lambda e: e["item"].lower())

    return {
        "list_name": list_name,
        "ideas": dated + undated,
        "shared": shared,
    }


def _count_unchecked(path: Path) -> int:
    if not path.exists():
        return 0
    active, _ = _split_archive(path.read_text())
    return sum(1 for l in active.split("\n") if l.startswith("- [ ]"))
