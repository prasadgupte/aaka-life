#!/usr/bin/env python3
"""
tools/inbox_worker.py — Scans 00-Inbox/{namespace}/ files, dispatches to router,
calls build_indexes after processing.

Files in inbox are plain text or markdown with optional YAML frontmatter.
The worker reads the filename + content as tags.

Usage:
  python3 tools/inbox_worker.py --namespace alice --dry-run
  python3 tools/inbox_worker.py   # process all namespaces (reads from aaka_config.carriers())
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))


def _vault_path() -> Path:
    from aaka_config import vault_path_for, default_actor
    return vault_path_for(default_actor())


def extract_tags_from_file(path: Path) -> list[str]:
    """Extract tags from filename (stem, split on [-_ ]) and any frontmatter tags field."""
    import re
    import yaml as _yaml

    tags = set()

    # From filename stem
    stem = path.stem.lower()
    for word in re.split(r"[-_ ]+", stem):
        if word:
            tags.add(word)

    # From frontmatter
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = __import__("re").match(r"^---\n(.*?)\n---", text, __import__("re").DOTALL)
        if m:
            fm = _yaml.safe_load(m.group(1)) or {}
            for t in (fm.get("tags") or []):
                tags.add(str(t).lower())
    except Exception:
        pass

    return list(tags)


def process_namespace(namespace: str, vault: Path, dry_run: bool = False) -> list[dict]:
    """Process all files in 00-Inbox/{namespace}/."""
    from tools.inbox_router import route

    inbox_dir = vault / "00-Inbox" / namespace
    if not inbox_dir.exists():
        return []

    unprocessed_dir = vault / "00-Inbox" / "unprocessed"
    results = []

    for f in sorted(inbox_dir.iterdir()):
        if f.is_dir() or f.name.startswith("."):
            continue

        tags = extract_tags_from_file(f)
        if not tags:
            continue

        outcome = route(tags, namespace=namespace, vault=vault, dry_run=dry_run)
        outcome["source_file"] = str(f.name)

        if outcome["unknown"] and not outcome["outcomes"]:
            # Fully unresolved — move to unprocessed
            if not dry_run:
                unprocessed_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(unprocessed_dir / f.name))
            outcome["disposition"] = "unprocessed"
        else:
            outcome["disposition"] = "routed"

        results.append(outcome)
        print(f"[inbox_worker] {f.name}: entities={list(outcome['entities'].keys())} "
              f"actions={list(outcome['actions'].keys())} "
              f"outcomes={len(outcome['outcomes'])} disposition={outcome['disposition']}")

    return results


def run(namespaces: list[str], vault: Path, dry_run: bool = False) -> None:
    from tools.build_indexes import build_all

    total_files = 0
    for ns in namespaces:
        results = process_namespace(ns, vault, dry_run=dry_run)
        total_files += len(results)

    # Rebuild indexes after processing
    if not dry_run:
        summary = build_all(vault)
        print(f"[inbox_worker] indexes rebuilt: scanned={summary['scanned_files']} "
              f"projects={summary['projects']} tags={summary['unique_tags']}")
    else:
        print("[inbox_worker] dry-run: skipping index rebuild")

    print(f"[inbox_worker] done — {total_files} file(s) processed across {len(namespaces)} namespace(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aaka inbox worker")
    parser.add_argument("--namespace", help="Process only this namespace (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Don't move files or write indexes")
    args = parser.parse_args()

    if args.namespace:
        import aaka_config as _cfg
        vault = _cfg.vault_path_for(args.namespace)
        run([args.namespace], vault=vault, dry_run=args.dry_run)
    else:
        import aaka_config as _cfg
        carrier_members = [m for m in _cfg.members() if m.get("is_carrier")] or [{"id": _cfg.default_actor()}]
        for m in carrier_members:
            ns = m["id"]
            vault = _cfg.vault_path_for(ns)
            run([ns], vault=vault, dry_run=args.dry_run)
