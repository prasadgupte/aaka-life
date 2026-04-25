#!/usr/bin/env python3
"""
sensor/error_digest.py — Actionable error digest for the aaka sensor.

Scans VPS log files, persists new error events to the error_events SQLite
table, and sends a digest of *unacknowledged* errors to admin members.

Called via cron (sensor/entrypoint.sh), daily at 07:30 UTC.

Public helpers (imported by sensor/intents/admin.py):
  scan_and_store(hours=24)      → dict of category → count newly inserted
  get_unacked_summary()         → list of dicts [{category, count, sample, first_seen}]
  ack_all()                     → int (rows acknowledged)
"""

import hashlib
import sys
import os
import re
import datetime
from pathlib import Path
from collections import Counter

BASE = Path(os.environ.get("AAKA_BASE", "/app"))
sys.path.insert(0, str(BASE))
import aaka_config

LOGS_DIR = aaka_config.LOGS_DIR
MAX_FILE_MB = 5
SKIP_FILE_MB = 50

_TS_PATTERNS = [
    re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"),
    re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),
]


def _admin_sender_ids() -> list[str]:
    ids = []
    for m in aaka_config.members():
        if m.get("role") not in ("admin", "adult"):
            continue
        sid = m.get("telegram") or m.get("telegram_id") or m.get("whatsapp_id") or m.get("sender_id", "")
        if sid:
            ids.append(str(sid))
    return ids


def _parse_ts(line: str) -> datetime.datetime | None:
    for pat in _TS_PATTERNS:
        m = pat.search(line)
        if m:
            ts_str = m.group(1).replace("T", " ")
            try:
                return datetime.datetime.fromisoformat(ts_str).replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                pass
    return None


def _categorize(filename: str, line: str) -> str | None:
    low = line.lower()
    if not any(k in low for k in ("error", "warn", "traceback", "exception", "failed", "failure")):
        return None
    if "telegram_poller" in filename:
        if "exited (code" in line or "restarting in" in line:
            return "poller_restart"
        # "read operation timed out" is normal long-poll expiry — not a real dispatch error
        if ("dispatch error" in low or "timeout" in low) and "timed out" not in low:
            return "dispatch_error"
        if any(f" {c}" in line for c in ("400", "429", "500", "502", "503")):
            return "tg_delivery"
        if "timed out" in low:
            return None  # expected long-poll expiry — not actionable
        return "tg_other"
    if "sidecar_sync" in filename or "audit" in filename:
        if "403" in line or "404" in line:
            return "cal_sync"
        if "warn" in low or "error" in low:
            return "cal_other"
        return None
    if "outbox_flush" in filename:
        if "[error]" in low or "error" in low:
            return "outbox_error"
        return None
    if "warn" in low or "error" in low:
        return "other"
    return None


def _scan_file(path: Path, since: datetime.datetime) -> tuple[Counter, dict]:
    """Scan a log file for errors since `since`.

    Returns:
      counts:  Counter of category → total occurrences
      samples: dict of category → first matching line
    """
    counts: Counter = Counter()
    samples: dict = {}
    size_mb = path.stat().st_size / 1_048_576
    if size_mb > SKIP_FILE_MB:
        counts["_oversize"] += 1
        return counts, samples
    mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc)
    if mtime < since:
        return counts, samples
    filename = path.name
    last_ts: datetime.datetime | None = None
    try:
        with path.open(errors="replace") as f:
            for line in f:
                line = line.rstrip()
                ts = _parse_ts(line)
                if ts:
                    last_ts = ts
                # Only count lines whose own timestamp (or the most recent preceding
                # timestamp on a multi-line traceback) falls within the window.
                # Lines before the first timestamp in a file are excluded — they
                # are almost always stale leftovers from earlier runs.
                if last_ts is None or last_ts < since:
                    continue
                cat = _categorize(filename, line)
                if cat:
                    counts[cat] += 1
                    if cat not in samples:
                        samples[cat] = line[:200]
    except Exception:
        counts["_read_error"] += 1
    return counts, samples


def _event_pk(category: str, date_bucket: str, source_file: str) -> str:
    raw = f"{category}|{date_bucket}|{source_file[:60]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _upsert_events(new_counts: "dict[str, Counter]", samples: "dict[str, dict]") -> int:
    """Write new error events to the error_events table. Returns total rows upserted."""
    from aaka_queue.queue import _connect
    conn = _connect()
    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_bucket = datetime.date.today().isoformat()
    try:
        week = datetime.datetime.strptime(date_bucket, "%Y-%m-%d").strftime("%G-W%V")
    except Exception:
        week = date_bucket[:7]

    inserted = 0
    for filename, counts in new_counts.items():
        for cat, n in counts.items():
            if cat.startswith("_"):
                continue
            pk = _event_pk(cat, date_bucket, filename)
            sample = samples.get(filename, {}).get(cat, "")
            conn.execute("""
                INSERT INTO error_events
                    (id, captured_at, last_seen_at, category, count, sample_line, source_file, acknowledged, week_bucket)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    count = count + excluded.count,
                    last_seen_at = excluded.last_seen_at
            """, (pk, now_ts, now_ts, cat, n, sample, filename, week))
            inserted += n
    conn.commit()
    return inserted


def scan_and_store(hours: int = 24) -> "Counter":
    """Scan log files and persist new error events. Returns category counts."""
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    all_counts: "dict[str, Counter]" = {}
    all_samples: "dict[str, dict]" = {}
    for log_file in sorted(LOGS_DIR.glob("*.log")):
        counts, samples = _scan_file(log_file, since)
        all_counts[log_file.name] = counts
        all_samples[log_file.name] = samples
    _upsert_events(all_counts, all_samples)
    merged: Counter = Counter()
    for c in all_counts.values():
        merged.update({k: v for k, v in c.items() if not k.startswith("_")})
    return merged


def get_unacked_summary() -> "list[dict]":
    """Return list of unacknowledged error categories with counts and samples."""
    from aaka_queue.queue import _connect
    conn = _connect()
    rows = conn.execute("""
        SELECT category, SUM(count) AS total, MIN(captured_at) AS first_seen,
               MIN(sample_line) AS sample
        FROM error_events
        WHERE acknowledged = 0
        GROUP BY category
        ORDER BY total DESC
    """).fetchall()
    return [
        {"category": r["category"], "count": r["total"],
         "first_seen": r["first_seen"], "sample": r["sample"] or ""}
        for r in rows
    ]


def ack_all() -> int:
    """Mark all unacknowledged error events as acknowledged. Returns row count."""
    from aaka_queue.queue import _connect
    conn = _connect()
    conn.execute(
        "UPDATE error_events SET acknowledged = 1 WHERE acknowledged = 0"
    )
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0]


def _file_size_warnings(logs_dir: Path) -> list[str]:
    warnings = []
    for f in sorted(logs_dir.glob("*.log")):
        mb = f.stat().st_size / 1_048_576
        if mb >= MAX_FILE_MB:
            warnings.append(f"{f.name} {mb:.1f}MB")
    return warnings


_LABELS = {
    "tg_delivery":    "Telegram delivery failures",
    "tg_other":       "Telegram other errors",
    "dispatch_error": "Router dispatch errors/timeouts",
    "poller_restart": "Poller restarts (crashes)",
    "cal_sync":       "Calendar sync 403/404 warnings",
    "cal_other":      "Calendar sync errors",
    "outbox_error":   "Outbox delivery failures",
    "other":          "Other errors",
}


def _format_digest_from_db(size_warnings: list[str]) -> str | None:
    """Format digest message from unacknowledged DB events. Returns None if clean."""
    summary = get_unacked_summary()
    lines = []
    total = 0
    for entry in summary:
        label = _LABELS.get(entry["category"], entry["category"])
        n = entry["count"]
        lines.append(f"• {label}: {n}")
        total += n
    if size_warnings:
        lines.append(f"• Logs ≥{MAX_FILE_MB}MB (no rotation): {', '.join(size_warnings)}")
    if not lines:
        return None
    date_str = datetime.date.today().isoformat()
    body = "\n".join(lines)
    return f"*Sensor Error Digest* ({date_str})\n\n{body}\n\nTotal: {total} issues\n_Reply /errors flush to clear_"


def main():
    counts = scan_and_store(hours=24)
    for k, n in counts.items():
        if n:
            print(f"[error_digest] {k}={n}")

    size_warnings = _file_size_warnings(LOGS_DIR)
    for w in size_warnings:
        print(f"[error_digest] large log: {w}")

    msg = _format_digest_from_db(size_warnings)
    if not msg:
        print("[error_digest] all clear — nothing to report")
        return

    try:
        from aaka_queue.queue import write_outbox
        for sid in _admin_sender_ids():
            write_outbox(channel_id=sid, sender=sid, text=msg)
        print(f"[error_digest] digest sent to {len(_admin_sender_ids())} admin(s)")
    except Exception as e:
        print(f"[error_digest] could not write outbox: {e}")


if __name__ == "__main__":
    main()
