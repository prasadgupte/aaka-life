#!/usr/bin/env python3
"""
Aaka — Natural language → PendingTask JSON extractor.
Zero-token path for metadata fields; LLM for title, due, and description.
"""

import re, json, datetime, sys
from pathlib import Path

import os
BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))

import aaka_config
from llm import call_llm

# ── Zero-token detection helpers ──────────────────────────────────────────────

URGENT_WORDS = re.compile(r'\b(critical|urgent|emergency|asap|immediately)\b', re.I)

def _detect_urgency(text: str) -> bool:
    return bool(URGENT_WORDS.search(text))


STAR_WORDS = re.compile(r'\b(important|imp|high.priority|star)\b|❗', re.I)

def _detect_starring(text: str, urgent: bool) -> bool:
    return urgent or bool(STAR_WORDS.search(text))


RECUR_WORDS = re.compile(
    r'\b(every|weekly|monthly|daily|each\s+week|each\s+month)\b', re.I
)

def _detect_recurring(text: str) -> bool:
    return bool(RECUR_WORDS.search(text))


def _extract_repeats(text: str) -> str | None:
    m = re.search(r'\b(every\s+\w+|weekly|monthly|daily|each\s+\w+)\b', text, re.I)
    return m.group(0) if m else None


def _detect_owner(text: str, sender_member_id: str) -> str:
    for m in aaka_config.members():
        if re.search(rf'@{re.escape(m["name"])}', text, re.I):
            return m["id"]
    return sender_member_id


def _load_verb_triggers() -> dict:
    import yaml
    tags_file = aaka_config.CONFIG_DIR / "config" / "tags.yaml"
    if not tags_file.exists():
        return {}
    cfg = yaml.safe_load(tags_file.read_text())
    return cfg.get("tag_map", {}).get("verb_triggers", {})


_VERB_TRIGGERS: dict | None = None

def _get_verb_triggers() -> dict:
    global _VERB_TRIGGERS
    if _VERB_TRIGGERS is None:
        _VERB_TRIGGERS = _load_verb_triggers()
    return _VERB_TRIGGERS


def _detect_tags(text: str) -> list[str]:
    tags: set[str] = set()
    for verb, tag_list in _get_verb_triggers().items():
        if re.search(rf'\b{re.escape(verb)}\w*\b', text, re.I):
            tags.update(tag_list)
    # Member-specific tag rules — add yours in aaka.yaml or extend _get_verb_triggers()
    # e.g. if re.search(r'\bChild1Name\b', text, re.I): tags.add('#school')
    for t in re.findall(r'#\w+', text):
        tags.add(t.lower())
    return sorted(tags)


# ── Display string builder ────────────────────────────────────────────────────

def _build_display_string(task: dict) -> str:
    parts = []
    if task.get("starred"):
        parts.append("⭐")
    if task.get("urgent"):
        parts.append("🔴")
    parts.append(task["title"])

    meta_parts = []
    if task.get("owner"):
        owner_name = ""
        for m in aaka_config.members():
            if m["id"] == task["owner"]:
                owner_name = m["name"]
                break
        if owner_name:
            meta_parts.append(f"@{owner_name}")
    if task.get("tags"):
        meta_parts.append(" ".join(task["tags"]))
    if meta_parts:
        parts.append("—")
        parts.append(" ".join(meta_parts))

    if task.get("due_date"):
        try:
            d = datetime.date.fromisoformat(task["due_date"])
            due_str = d.strftime("%b %-d")
            parts.append(f"— due {due_str}")
        except ValueError:
            pass

    if task.get("recurring"):
        parts.append("🔄")

    return " ".join(parts)


# ── LLM extraction ────────────────────────────────────────────────────────────

def _llm_extract(text: str) -> dict:
    today = datetime.date.today().isoformat()
    prompt = f"""Extract a task from the message below. Return JSON with exactly three keys:
- "title": short imperative task title (≤ 60 chars, required)
- "due": ISO 8601 date string in UTC (e.g. "2026-03-10") if a due date is mentioned, else null
- "description": any additional context beyond the core title, or ""

Today is {today}. Respond with only the JSON object, no explanation.

Message: {text}"""
    raw = call_llm(prompt).strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    return json.loads(raw)


# ── Main entry point ──────────────────────────────────────────────────────────

def prepare_task(text: str, sender_member_id: str = "") -> dict:
    """Convert natural language task description to a PendingTask dict.

    Accepts either raw JSON (from a Gem fast path) or natural language.

    Args:
        text: stripped task description (command prefix already removed)
        sender_member_id: member id of the sender (as configured in aaka.yaml, e.g. "alice")

    Returns:
        PendingTask dict ready to be written to .pending_task.json
    """
    # JSON fast path — Gem output bypasses zero-token detection and LLM entirely
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            task = json.loads(stripped)
            if "title" in task:
                task.setdefault("initiated_by", sender_member_id)
                task.setdefault("urgent", False)
                task.setdefault("starred", False)
                task.setdefault("tags", [])
                task.setdefault("due_date", None)
                task.setdefault("recurring", False)
                task.setdefault("repeats", None)
                task.setdefault("owner", sender_member_id)
                task.setdefault("list", "My Tasks")
                task.setdefault("description", "")
                task.setdefault("duration", "")
                task.setdefault("priority_icon",
                    "🔴" if task["urgent"] else ("🟡" if task["starred"] else ""))
                if "display_string" not in task:
                    task["display_string"] = _build_display_string(task)
                return task
        except (json.JSONDecodeError, KeyError):
            pass  # fall through to NL path

    urgent    = _detect_urgency(text)
    starred   = _detect_starring(text, urgent)
    recurring = _detect_recurring(text)
    repeats   = _extract_repeats(text) if recurring else None
    owner     = _detect_owner(text, sender_member_id)
    tags      = _detect_tags(text)

    dur_m = re.search(r'#?(\d+)\s*(h(?:ours?)?|m(?:in(?:utes?)?)?)\b', text, re.I)
    if dur_m:
        num, unit = int(dur_m.group(1)), dur_m.group(2).lower()
        duration = f"{num}h" if unit.startswith("h") else f"{num}m"
    else:
        duration = ""

    priority_icon = "🔴" if urgent else ("🟡" if starred else "")

    try:
        extracted = _llm_extract(text)
    except Exception as e:
        raise RuntimeError(f"LLM extraction failed: {e}") from e

    raw_title = (extracted.get("title") or text).strip()
    description = (extracted.get("description") or "").strip()

    # Overflow protection: truncate title at 100 chars, append to description
    if len(raw_title) > 100:
        overflow = raw_title[100:].strip()
        raw_title = raw_title[:100].strip()
        description = (overflow + " " + description).strip() if description else overflow

    due_raw  = extracted.get("due") or None
    due_date = None
    if due_raw:
        # Accept "YYYY-MM-DD" or full RFC3339 — normalise to date-only
        due_date = due_raw[:10] if len(due_raw) >= 10 else None

    if recurring and not due_date:
        due_date = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    # Resolve list name (always "My Tasks" for now)
    list_name = "My Tasks"

    task: dict = {
        "title":         raw_title,
        "description":   description,
        "urgent":        urgent,
        "starred":       starred,
        "priority_icon": priority_icon,
        "tags":          tags,
        "owner":         owner,
        "due_date":      due_date,
        "recurring":     recurring,
        "repeats":       repeats,
        "list":          list_name,
        "initiated_by":  sender_member_id,
        "duration":      duration,
    }
    task["display_string"] = _build_display_string(task)
    return task


def prepare_tasks(json_text: str, sender_member_id: str = "") -> list[dict]:
    """Parse a JSON array of task objects into a list of PendingTask dicts.

    Each item in the array is processed through prepare_task() (JSON fast path).
    Also accepts a single object and wraps it in a list.

    Args:
        json_text: JSON array string (from Gem batch output)
        sender_member_id: camelCase member id of the sender

    Returns:
        List of PendingTask dicts
    """
    items = json.loads(json_text)
    if not isinstance(items, list):
        items = [items]
    return [prepare_task(json.dumps(t), sender_member_id) for t in items]


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) if sys.argv[1:] else sys.stdin.read().strip()
    result = prepare_task(text, "user")
    print(json.dumps(result, indent=2, ensure_ascii=False))
