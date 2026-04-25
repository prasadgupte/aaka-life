#!/usr/bin/env python3
"""
Import a LastPass CSV export into the encrypted password store.

LastPass export columns: url, username, password, extra, name, grouping, fav

Usage:
    python3 tools/pw_import.py /path/to/lastpass_export.csv
    python3 tools/pw_import.py /path/to/lastpass_export.csv --overwrite   # update duplicates
    python3 tools/pw_import.py /path/to/lastpass_export.csv --dry-run     # preview only

The CSV can be exported from LastPass:
  Account Settings → Advanced → Export
"""

import sys
import csv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.password_store import PasswordStore, VAULT_FILE


def preview(csv_path: str) -> None:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"Found {len(rows)} entries in {csv_path}")
    groups: dict[str, int] = {}
    for row in rows:
        g = row.get("grouping", "") or "(none)"
        groups[g] = groups.get(g, 0) + 1
    print("Groups:")
    for g, count in sorted(groups.items()):
        print(f"  {g}: {count}")
    print()
    print("First 5 entries (name, url, username):")
    for row in rows[:5]:
        print(f"  {row.get('name','?'):40s}  {row.get('url',''):30s}  {row.get('username','')}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    csv_path = sys.argv[1]
    overwrite = "--overwrite" in sys.argv
    dry_run = "--dry-run" in sys.argv

    if not Path(csv_path).exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    if dry_run:
        preview(csv_path)
        sys.exit(0)

    if not VAULT_FILE.exists():
        print(f"Vault not found: {VAULT_FILE}")
        print("Run: python3 tools/pw_setup.py")
        sys.exit(1)

    store = PasswordStore()

    before = store.count()
    print(f"Vault has {before} existing entries.")
    print(f"Importing from: {csv_path}")
    if overwrite:
        print("Mode: overwrite duplicates")
    else:
        print("Mode: skip duplicates (use --overwrite to update them)")
    print()

    imported = store.import_lastpass_csv(csv_path, overwrite=overwrite)
    after = store.count()

    print(f"Imported: {imported} entries")
    print(f"Vault total: {after} entries ({after - before} net new)")
    print()
    print("Verify with:")
    print("  python3 -c \"from tools.password_store import PasswordStore; "
          "s=PasswordStore(); print(s.count(), 'entries')\"")


if __name__ == "__main__":
    main()
