#!/usr/bin/env python3
"""
tools/egress_report.py — CLI report of recent outbound message activity.

Reads logs/egress.jsonl and logs/ingress.jsonl to answer:
  - What messages were sent in the last N hours?
  - Who sent them, on which channel, from which source?
  - Any errors?
  - Full trace for a specific message hash?

Usage:
    python3 tools/egress_report.py                # last 1 hour
    python3 tools/egress_report.py --hours 24     # last 24 hours
    python3 tools/egress_report.py --trace abc123 # trace a specific correlation_id or content_hash
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config


def _config_dir() -> Path:
    return Path(os.environ.get("AAKA_CONFIG_DIR") or aaka_config.CONFIG_DIR)


def _logs_dir() -> Path:
    return _config_dir() / "logs"


def _read_log(name: str, cutoff: str) -> list[dict]:
    log_path = _logs_dir() / name
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text().splitlines():
        try:
            entry = json.loads(line)
            if entry.get("ts", "") >= cutoff:
                entries.append(entry)
        except Exception:
            pass
    return entries


def cmd_summary(hours: int) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    egress = _read_log("egress.jsonl", cutoff)
    ingress = [e for e in _read_log("ingress.jsonl", cutoff)
               if e.get("status") not in ("intent_resolved",)]

    print(f"\n=== Egress report: last {hours}h ({cutoff} → now) ===\n")

    # Egress summary
    total = len(egress)
    by_status: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    by_source: dict[str, int] = {}
    errors = []
    for e in egress:
        s = e.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1
        ch = e.get("channel", "?")
        by_channel[ch] = by_channel.get(ch, 0) + 1
        src = e.get("source", "?")
        by_source[src] = by_source.get(src, 0) + 1
        if s == "error":
            errors.append(e)

    print(f"Outbound: {total} total")
    for s, n in sorted(by_status.items()):
        print(f"  {s}: {n}")
    if by_channel:
        print(f"\nBy channel: " + ", ".join(f"{k}={v}" for k, v in sorted(by_channel.items())))
    if by_source:
        print(f"By source:  " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))

    if errors:
        print(f"\n⚠️  {len(errors)} error(s):")
        for e in errors[-10:]:
            print(f"  {e.get('ts','')} [{e.get('source','?')}] → {e.get('recipient','?')}: {e.get('error','')[:100]}")

    # Ingress summary
    print(f"\nInbound: {len(ingress)} total")
    in_by_status: dict[str, int] = {}
    in_by_channel: dict[str, int] = {}
    for e in ingress:
        s = e.get("status", "?")
        in_by_status[s] = in_by_status.get(s, 0) + 1
        ch = e.get("channel", "?")
        in_by_channel[ch] = in_by_channel.get(ch, 0) + 1
    if in_by_status:
        for s, n in sorted(in_by_status.items()):
            print(f"  {s}: {n}")
    if in_by_channel:
        print(f"By channel: " + ", ".join(f"{k}={v}" for k, v in sorted(in_by_channel.items())))

    print()


def cmd_trace(hash_prefix: str) -> None:
    """Print all log entries (ingress + egress) matching a correlation_id or content_hash prefix."""
    entries: list[dict] = []
    for log_name in ("ingress.jsonl", "egress.jsonl"):
        log_path = _logs_dir() / log_name
        if not log_path.exists():
            continue
        for line in log_path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            cid = entry.get("correlation_id", "")
            chash = entry.get("content_hash", "")
            if cid.startswith(hash_prefix) or chash.startswith(hash_prefix):
                entry["_log"] = log_name
                entries.append(entry)

    entries.sort(key=lambda e: e.get("ts", ""))

    if not entries:
        print(f"No trace found for {hash_prefix!r}")
        return

    print(f"\n=== Trace: {hash_prefix} ({len(entries)} entries) ===\n")
    for e in entries:
        direction = "→" if e["_log"] == "egress.jsonl" else "←"
        cid = e.get("correlation_id", "")
        status = e.get("status", "?")
        source = e.get("source", "?")
        intent = f" [{e['intent']}]" if e.get("intent") else ""
        error = f" ERROR: {e['error']}" if e.get("error") else ""
        print(f"  {direction} {e.get('ts','')}  [{source}]  {status}{intent}{error}  cid={cid[:12]}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Egress/ingress activity report")
    parser.add_argument("--hours", type=int, default=1,
                        help="Report window in hours (default: 1)")
    parser.add_argument("--trace", metavar="HASH",
                        help="Show full trace for a correlation_id or content_hash prefix")
    args = parser.parse_args()

    if args.trace:
        cmd_trace(args.trace)
    else:
        cmd_summary(args.hours)


if __name__ == "__main__":
    main()
