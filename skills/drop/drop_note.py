"""
skills/drop/drop_note.py — Executor handler for drop_note intent.

Appends notes to a per-topic log file in vault/Notes/<namespace>/.
The primary tag (first tag) determines the log filename.
Each note is a single timestamped line — no file explosion.
"""

import os
from datetime import date
from pathlib import Path

import aaka_config


def execute(payload: dict) -> dict:
    """Append a note entry to the topic log file.

    Payload keys:
        tags: list[str]       — routing tags; first = topic/filename
        body: str             — note body text
        namespace: str        — member id (e.g. "alice")
        message_id: str|None  — for reply threading
        source: str           — channel name
    """
    tags = payload.get("tags", [])
    body = payload.get("body", "").strip()
    namespace = payload.get("namespace", "user")

    if not body:
        raise ValueError("Note body is empty")

    vault = aaka_config.vault_path_for(namespace)
    notes_dir = vault / "04-Notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    # Primary tag = filename, remaining = sub-tags on the entry line
    primary = tags[0] if tags else "inbox"
    sub_tags = tags[1:] if len(tags) > 1 else []
    today = date.today().isoformat()

    log_path = notes_dir / f"{primary}.md"
    is_new = not log_path.exists()

    # Build entry line: - 2026-04-24 `sub tags` body text
    if sub_tags:
        tag_str = " ".join(sub_tags)
        entry = f"- {today} `{tag_str}` {body}\n"
    else:
        entry = f"- {today} {body}\n"

    with open(log_path, "a") as f:
        if is_new:
            f.write(f"---\ntags: [{primary}]\nowner: {namespace}\ntype: log\n---\n\n")
        f.write(entry)

    rel_path = str(log_path.relative_to(vault))
    return {
        "status": "ok",
        "file": rel_path,
        "tags": tags,
        "message_id": payload.get("message_id"),
    }
