#!/usr/bin/env python3
"""
Aaka — Skill Audit Report

Reads $LOGS_DIR/skills/skill-audit.log and prints a summary:
  - total executions by skill
  - success rate
  - last N errors

Usage:
  python3 tools/audit_report.py
  python3 tools/audit_report.py --errors 20
"""

import os
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))


def load_entries(log_path: Path) -> list[dict]:
    entries = []
    if not log_path.exists():
        return entries
    for line in log_path.read_text().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 5:
            ts, intent, skill_key, status, result_hash = parts
            entries.append({"ts": ts, "intent": intent, "skill": skill_key,
                            "status": status, "hash": result_hash})
    return entries


def report(log_path: Path, last_n_errors: int = 10) -> None:
    entries = load_entries(log_path)
    if not entries:
        print(f"No audit entries found at {log_path}")
        return

    by_skill: dict[str, dict] = defaultdict(lambda: {"ok": 0, "error": 0})
    errors = []

    for e in entries:
        by_skill[e["skill"]][e["status"]] = by_skill[e["skill"]].get(e["status"], 0) + 1
        if e["status"] == "error":
            errors.append(e)

    print(f"Skill Audit Report  ({len(entries)} total entries)")
    print("=" * 60)
    print(f"{'Skill':<30} {'OK':>6} {'ERR':>6} {'Rate':>8}")
    print("-" * 60)
    for skill, counts in sorted(by_skill.items()):
        ok = counts.get("ok", 0)
        err = counts.get("error", 0)
        total = ok + err
        rate = f"{100*ok//total}%" if total else "n/a"
        print(f"{skill:<30} {ok:>6} {err:>6} {rate:>8}")

    if errors:
        print(f"\nLast {min(last_n_errors, len(errors))} errors:")
        for e in errors[-last_n_errors:]:
            print(f"  {e['ts']}  {e['skill']}  {e['hash']}")


if __name__ == "__main__":
    import argparse
    import aaka_config

    parser = argparse.ArgumentParser(description="Skill audit report")
    parser.add_argument("--errors", type=int, default=10, help="Number of recent errors to show")
    parser.add_argument("--log", default=str(aaka_config.LOGS_DIR / "skills" / "skill-audit.log"))
    args = parser.parse_args()

    report(Path(args.log), last_n_errors=args.errors)
