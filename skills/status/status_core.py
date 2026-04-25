"""
Shared status functions used by both the sensor (via /status) and admin CLI.
"""

import os
import sqlite3
from pathlib import Path


def status_code() -> str:
    """Return sensor version and executor last-sync info."""
    config_dir = os.environ.get("AAKA_CONFIG_DIR", "/config")
    version_file = Path(config_dir) / "data" / ".sensor_version"
    if version_file.exists():
        version_info = version_file.read_text().strip()
    else:
        version_info = "deployed=unknown\ncommit=unknown"

    last_check_file = Path(config_dir) / "logs" / "last_executor_check"
    last_sync = last_check_file.read_text().strip() if last_check_file.exists() else "(not yet run)"

    lines = ["Code versions", "─────────────"]
    for line in version_info.splitlines():
        lines.append(f"Sensor  {line}")
    lines.append(f"Executor last sync: {last_sync}")
    return "\n".join(lines)


def status_queue() -> str:
    """Return a compact queue summary from butler.db."""
    config_dir = os.environ.get("AAKA_CONFIG_DIR", "/config")
    db_path = os.environ.get(
        "QUEUE_DB",
        str(Path(config_dir) / "data" / "queue" / "butler.db"),
    )
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM queue_items GROUP BY status"
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}

        recent = conn.execute(
            "SELECT intent, status, updated_at FROM queue_items ORDER BY updated_at DESC LIMIT 3"
        ).fetchall()

        tasks_todo = conn.execute(
            "SELECT COUNT(*) as n FROM tasks WHERE status='todo'"
        ).fetchone()
        tasks_n = tasks_todo["n"] if tasks_todo else "?"

        conn.close()
    except Exception as exc:
        return f"⚠️ Queue DB error: {exc}"

    last_check_file = Path(config_dir) / "logs" / "last_executor_check"
    last_sync = last_check_file.read_text().strip() if last_check_file.exists() else "(not yet run)"

    lines = [
        "Queue summary",
        "─────────────",
        f"awaiting confirm : {counts.get('awaiting_confirm', 0)}",
        f"confirmed (queued): {counts.get('confirmed', 0)}",
        f"done (all time)  : {counts.get('done', 0)}",
        f"error            : {counts.get('error', 0)}",
        f"tasks todo       : {tasks_n}",
        "",
        f"Last executor sync: {last_sync}",
    ]
    if recent:
        lines.append("Recent:")
        for r in recent:
            lines.append(f"  {r['intent']:<16} {r['status']:<12} {r['updated_at']}")
    return "\n".join(lines)
