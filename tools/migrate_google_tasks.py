#!/usr/bin/env python3
"""
tools/migrate_google_tasks.py — One-time migration: Google Tasks → aaka.db

Reads all tasks from all Google Task lists and inserts them into the tasks table
in butler.db. Idempotent: skips tasks whose title+namespace already exist.

Usage:
  python3 tools/migrate_google_tasks.py --namespace alice [--dry-run]
"""

import argparse
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))


def _google_tasks_client():
    from aaka_config import TOKENS_DIR
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    token_file = TOKENS_DIR / "token.json"
    creds = Credentials.from_authorized_user_file(str(token_file))
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _existing_titles(namespace: str) -> set[str]:
    import sqlite3
    from aaka_config import CONFIG_DIR
    db = os.environ.get("QUEUE_DB", str(CONFIG_DIR / "data" / "queue" / "butler.db"))
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT title FROM tasks WHERE namespace=?", (namespace,)
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def migrate(namespace: str, dry_run: bool = False) -> int:
    service = _google_tasks_client()
    existing = _existing_titles(namespace)

    lists_result = service.tasklists().list(maxResults=100).execute()
    task_lists = lists_result.get("items", [])

    imported = 0
    for tl in task_lists:
        tasks_result = service.tasks().list(
            tasklist=tl["id"], showCompleted=False, maxResults=100
        ).execute()
        for t in tasks_result.get("items", []):
            title = t.get("title", "").strip()
            if not title or title in existing:
                continue
            notes = t.get("notes", "") or ""
            due_date = (t.get("due", "") or "")[:10]  # ISO date only

            if dry_run:
                print(f"[dry-run] would import: '{title}' due={due_date or '—'}")
            else:
                from skills.task_manager import create_task
                create_task(
                    title=title,
                    namespace=namespace,
                    notes=notes,
                    due_date=due_date,
                    source="google_tasks_migration",
                )
                existing.add(title)
            imported += 1

    return imported


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Google Tasks → aaka.db")
    parser.add_argument("--namespace", default="alice", help="Member namespace (ID from aaka.yaml)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    n = migrate(namespace=args.namespace, dry_run=args.dry_run)
    verb = "Would import" if args.dry_run else "Imported"
    print(f"{verb} {n} task(s) into namespace={args.namespace}")
