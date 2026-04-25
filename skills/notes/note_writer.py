"""
skills/notes/note_writer.py — Sensor-side note storage.

Appends timestamped entries to per-topic log files in
$AAKA_CONFIG_DIR/data/notes/<topic>.md. Synced to vault via file_sync.

Routed topics (those with an entry in references.yaml routes) sync to
<route>/_context.md instead of 04-Notes/<topic>.md.
"""

import os
import re
from datetime import date
from pathlib import Path


def _notes_dir() -> Path:
    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
    d = config_dir / "data" / "notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _note_path(topic: str) -> Path:
    safe = re.sub(r'[^a-z0-9-]', '-', topic.lower()).strip('-')
    if not safe:
        safe = "inbox"
    return _notes_dir() / f"{safe}.md"


def _load_refs() -> dict:
    """Load references.yaml for the sensor (VPS-safe, no vault access needed).

    Uses the sensor-side copy synced from vault/99-System/references.yaml.
    Falls back to empty refs if not found.
    """
    try:
        from tools.inbox_router import load_sensor_references
        return load_sensor_references()
    except Exception:
        return {"entities": {}, "actions": {}, "aliases": {}, "routes": {}}


def resolve_alias(topic: str) -> str:
    """Resolve a topic through references.yaml aliases → canonical name."""
    try:
        from tools.inbox_router import resolve_topic_alias
        refs = _load_refs()
        return resolve_topic_alias(topic, refs)
    except Exception:
        return topic.lower()


def append_note(topic: str, body: str, namespace: str = "user",
                alias_name: "str | None" = None) -> tuple:
    """Append a timestamped entry to a topic log file.

    If alias_name is provided (from !name syntax), the topic is stored
    under that canonical name and an alias entry is written to references.yaml.

    Supports multi-line body (each non-blank line → separate entry) and
    heading syntax: body starting with '#' writes a markdown section header.

    Returns (relative_path, formatted_lines, entry_count_after) where
    formatted_lines are the strings written (without trailing newline) and
    entry_count_after is the total number of '- ' entry lines in the file.
    """
    # Resolve alias: if !name was provided, use that as the canonical name
    # and register the alias. Otherwise resolve through references.yaml.
    if alias_name:
        canonical = alias_name.lower()
        canonical = re.sub(r'[^a-z0-9-]', '-', canonical).strip('-') or topic.lower()
        # Register alias in references.yaml (executor-side, best-effort via sensor copy)
        _register_alias_in_vault(topic, canonical, namespace)
    else:
        canonical = resolve_alias(topic)

    path = _note_path(canonical)
    is_new = not path.exists()
    today = date.today().isoformat()

    # Split multi-line body; each non-blank line becomes its own entry
    raw_lines = [l.strip() for l in body.split('\n') if l.strip()]
    formatted = []
    for line in raw_lines:
        if line.startswith('#'):
            heading = line.lstrip('#').strip()
            formatted.append(f"\n## {heading}")
        else:
            formatted.append(f"- {today} {line}")

    with open(path, "a") as f:
        if is_new:
            f.write(f"---\ntags: [{canonical}]\nowner: {namespace}\ntype: log\n---\n\n")
        for fmt in formatted:
            f.write(fmt + "\n")

    all_lines = path.read_text().splitlines()
    entry_count = sum(1 for l in all_lines if l.startswith("- "))

    return f"data/notes/{path.name}", formatted, entry_count


def _register_alias_in_vault(alias: str, canonical: str, namespace: str) -> None:
    """Register an alias in $AAKA_CONFIG_DIR/config/references.yaml."""
    try:
        from tools.inbox_router import write_tag_to_references
        write_tag_to_references(alias=alias, alias_target=canonical)
    except Exception:
        pass


def describe() -> str:
    return (
        "📝 *Notes* — per-topic append-only logs\n\n"
        "  n <topic>            — read last 10 entries\n"
        "  n <topic> <N>        — read last N entries\n"
        "  n <topic> <text>     — write a new entry\n"
        "  n <topic> #heading   — write a section heading\n"
        "  n <topic> !name      — set canonical filename alias\n"
        "  /notes               — list all topics\n\n"
        "Multi-line: one entry per line after the topic\n\n"
        "Examples:\n"
        "  n fin 2025 claim boston taxi\n"
        "  n jp #trains\n"
        "  n todo call dentist\n"
        "  n ortho !ari-ortho-treatment first visit"
    )


def sync_dest(topic: str, namespace: str = "user") -> str:
    """Return the vault-relative destination path for file_sync.

    If the topic (after alias resolution) has a route in references.yaml,
    the destination is <route>/_context.md (unified context file).
    Otherwise falls back to 04-Notes/<topic>.md (legacy behavior).

    Vault root is per-member (vault_path_for(namespace)), so no namespace
    subfolder is needed.
    """
    canonical = resolve_alias(topic)
    try:
        from tools.inbox_router import resolve_topic_route
        refs = _load_refs()
        route_path = resolve_topic_route(canonical, refs)
        if route_path:
            return f"{route_path}/_context.md"
    except Exception:
        pass
    path = _note_path(canonical)
    return f"04-Notes/{path.name}"
