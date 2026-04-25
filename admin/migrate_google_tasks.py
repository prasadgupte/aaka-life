#!/usr/bin/env python3
"""
admin/migrate_google_tasks.py — One-shot migration from Google Tasks → local JSON store.

Usage (run on Mac):
  python3 admin/migrate_google_tasks.py [--dry-run]

Steps:
  1. Fetch all open tasks from Google Tasks via token_vps.json
  2. Convert to local_tasks format and append to data/tasks/tasks.json
  3. Mark them all complete in Google Tasks
  4. Print a summary

Safe to re-run: skips tasks already present in local store (by title match).
"""

import sys, os, argparse
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config

TOKENS_DIR = aaka_config.TOKENS_DIR
VPS_TOKEN  = TOKENS_DIR / "token_vps.json"


def main():
    parser = argparse.ArgumentParser(description="Migrate Google Tasks → local JSON store")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    args = parser.parse_args()

    if not VPS_TOKEN.exists():
        # Try member token as fallback
        auth_ms = aaka_config.auth_members()
        token_path = None
        for m in auth_ms:
            info = aaka_config.auth_for(m["id"])
            if info and info["token_file"].exists():
                scopes = info.get("scopes", [])
                if any("tasks" in s for s in scopes):
                    token_path = info["token_file"]
                    break
        if not token_path:
            print("❌ No token with tasks scope found. Run: python3 admin/reauth.py --profile vps")
            sys.exit(1)
    else:
        token_path = VPS_TOKEN

    print(f"Using token: {token_path.name}")

    # ── 1. Fetch from Google Tasks ─────────────────────────────────────────────
    from skills.tasks.tasks import list_tasks, complete_task
    print("Fetching open tasks from Google Tasks…")
    google_tasks = list_tasks(token_file=token_path)
    if not google_tasks:
        print("No open tasks in Google Tasks. Nothing to migrate.")
        return

    print(f"Found {len(google_tasks)} open task(s):")
    for t in google_tasks:
        print(f"  - {t['title']}" + (f"  (due {t['due'][:10]})" if t.get("due") else ""))

    # ── 2. Load existing local tasks (dedup by title) ──────────────────────────
    from skills.tasks.local_tasks import _load, _save, add_task
    existing = _load()
    existing_titles = {t["title"].lower() for t in existing}

    # Also deduplicate within the Google Tasks list itself (keep first occurrence)
    seen_google_titles: set = set()
    deduped_google_tasks = []
    for gt in google_tasks:
        key = gt["title"].lower()
        if key not in seen_google_titles:
            seen_google_titles.add(key)
            deduped_google_tasks.append(gt)
    if len(deduped_google_tasks) < len(google_tasks):
        print(f"  (deduped {len(google_tasks) - len(deduped_google_tasks)} duplicate(s) within Google Tasks)")
    google_tasks = deduped_google_tasks

    to_migrate = []
    skipped = []
    for gt in google_tasks:
        if gt["title"].lower() in existing_titles:
            skipped.append(gt["title"])
        else:
            to_migrate.append(gt)

    if skipped:
        print(f"\nSkipping {len(skipped)} already-present task(s):")
        for s in skipped:
            print(f"  - {s}")

    if not to_migrate:
        print("\nAll tasks already in local store. Nothing to migrate.")
        return

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Migrating {len(to_migrate)} task(s)…")

    # ── 3. Convert + write local ───────────────────────────────────────────────
    from skills.tasks.tasks import _parse_title_meta
    import re, datetime

    migrated_ids = []
    for gt in to_migrate:
        meta = _parse_title_meta(gt["title"])
        notes = gt.get("notes", "")
        # Strip duration marker + attribution from notes
        description = re.sub(r'\s*⏲️\S+', '', notes).removesuffix(" 🌤️").removesuffix("🌤️").strip()

        due_date = ""
        if gt.get("due"):
            due_date = gt["due"][:10]

        payload = {
            "title":       gt["title"],
            "owner":       meta.get("owner", ""),
            "due_date":    due_date,
            "urgent":      meta.get("urgent", False),
            "starred":     meta.get("starred", False),
            "tags":        meta.get("tags", []),
            "recurring":   meta.get("recurring", False),
            "description": description,
        }
        if not args.dry_run:
            task = add_task(payload)
            migrated_ids.append((gt["id"], task["id"], gt["title"]))
            print(f"  ✓ {gt['title']}")
        else:
            print(f"  [dry-run] would add: {gt['title']}")

    # ── 4. Mark complete in Google Tasks ──────────────────────────────────────
    if not args.dry_run and migrated_ids:
        print(f"\nMarking {len(migrated_ids)} task(s) complete in Google Tasks…")
        errors = []
        for google_id, local_id, title in migrated_ids:
            try:
                complete_task(google_id, token_file=token_path)
                print(f"  ✓ {title}")
            except Exception as e:
                errors.append((title, str(e)))
                print(f"  ✗ {title}: {e}")
        if errors:
            print(f"\n⚠️  {len(errors)} task(s) could not be marked complete in Google Tasks.")
            print("   They remain open there but are now in the local store.")

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Done. {len(to_migrate)} task(s) migrated.")
    if not args.dry_run:
        print(f"Local store: {aaka_config.TASKS_DIR / 'tasks.json'}")


if __name__ == "__main__":
    main()
