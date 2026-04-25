"""
sensor/helpers.py — shared queue-sync helpers used by router_sensor.py and
sensor/intents/ domain modules.

Kept separate to avoid circular imports: domain modules → helpers → aaka_queue
(never the other way around).
"""
import os
from pathlib import Path

import aaka_config
from aaka_queue.queue import write_item, update_status


def queue_file_sync(list_name: str, sender: str, channel_id: str, source: str = "") -> None:
    """Queue a file_sync item for a list file, deduplicating against pending syncs."""
    import sqlite3
    from skills.lists.list_manager import _resolve_list, is_shared, _safe_name

    namespace = _namespace_for_sender(sender) or "shared"
    resolved = _resolve_list(list_name, namespace)
    lists_dir_name = "data/lists"
    source_path = f"{lists_dir_name}/{resolved.parent.name}/{_safe_name(list_name)}.md"
    if is_shared(list_name):
        dest_path = f"Notes/_shared/lists/{_safe_name(list_name)}.md"
    else:
        dest_path = f"Notes/{namespace}/lists/{_safe_name(list_name)}.md"

    db_path = os.environ.get("QUEUE_DB", str(
        Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "data" / "queue" / "butler.db"
    ))
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        existing = conn.execute(
            "SELECT id FROM queue_items WHERE intent='file_sync'"
            " AND status IN ('confirmed','executing')"
            " AND json_extract(payload, '$.source_path') = ?",
            (source_path,),
        ).fetchone()
        conn.close()
        if existing:
            return
    except Exception:
        pass

    payload = {
        "source_path": source_path,
        "dest_vault_path": dest_path,
        "namespace": namespace,
    }
    item_id = write_item(
        intent="file_sync", raw_message=f"sync {list_name}",
        sender=sender, channel_id=channel_id, source=source,
        payload=payload,
    )
    update_status(item_id, "confirmed")


def queue_note_sync(source_path: str, dest_path: str, sender: str, channel_id: str) -> None:
    """Queue a file_sync item for an arbitrary source → dest vault path pair."""
    namespace = _namespace_for_sender(sender) or "shared"
    payload = {
        "source_path": source_path,
        "dest_vault_path": dest_path,
        "namespace": namespace,
    }
    item_id = write_item(
        intent="file_sync", raw_message=f"sync {source_path}",
        sender=sender, channel_id=channel_id, source="sensor",
        payload=payload,
    )
    update_status(item_id, "confirmed")


def _namespace_for_sender(sender: str) -> "str | None":
    m = aaka_config.member_by_sender(sender)
    return m["id"] if m and m.get("id") else None
