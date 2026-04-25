#!/usr/bin/env python3
"""
sensor/gmail_poller.py — Gmail label ingest cron.

Watches Gmail labels configured in aaka.yaml (gmail_labels block) and writes
queue items for each new unread email. No LLM, no routing — the executor
(_exec_gmail_label in queue_worker.py) handles extraction and output.

Usage:
  python3 sensor/gmail_poller.py                            # production run
  python3 sensor/gmail_poller.py --dry-run                  # report without writing queue items
  python3 sensor/gmail_poller.py --reprocess "Travel/AAKA-JP"           # clear seen state + re-queue
  python3 sensor/gmail_poller.py --reprocess "Travel/AAKA-JP" --dry-run # preview re-queue

--reprocess clears seen_ids for the named label so all currently unread+labeled emails are
treated as new. Emails must be unread in Gmail (mark them unread first if needed).

Cron (via entrypoint.sh):
  */10 * * * * . /etc/aaka-cron-env && cd /app && python3 sensor/gmail_poller.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent
)
sys.path.insert(0, str(BASE))

import aaka_config
from aaka_queue.queue import write_item, update_status
from skills.mail.gmail import get_gmail_service, list_by_label, fetch_full


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_path(label: str) -> Path:
    safe = re.sub(r"[^a-z0-9]", "-", label.lower()).strip("-") or "inbox"
    d = aaka_config.DATA_DIR / "gmail"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"seen_{safe}.json"


def _load_state(label: str) -> "dict | None":
    path = _state_path(label)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _save_state(label: str, state: dict) -> None:
    _state_path(label).write_text(json.dumps(state, indent=2))


def _ingest_label(svc, label_cfg: dict, dry_run: bool) -> None:
    label = label_cfg["label"]
    state = _load_state(label)

    try:
        messages = list_by_label(svc, label, max_results=20)
    except Exception as exc:
        print(f"[gmail_poller] {label}: Gmail API error — {exc}")
        return

    msg_ids = [m["id"] for m in messages]

    if state is None:
        # First run: bootstrap — mark all as seen without processing (avoid backlog flood)
        if not dry_run:
            _save_state(label, {"seen_ids": msg_ids, "last_poll": _now()})
        print(f"[gmail_poller] {label}: bootstrap {len(msg_ids)} message(s) — no processing")
        return

    seen_set = set(state.get("seen_ids", []))
    new_msgs = [m for m in messages if m["id"] not in seen_set]

    if not new_msgs:
        print(f"[gmail_poller] {label}: 0 new messages")
        state["last_poll"] = _now()
        if not dry_run:
            _save_state(label, state)
        return

    group_id = os.environ.get("TELEGRAM_GROUP_ID", "")

    for msg in new_msgs:
        try:
            full = fetch_full(svc, msg["id"])
        except Exception as exc:
            print(f"[gmail_poller] {label}: fetch error for {msg['id']} — {exc}")
            continue

        subject_short = full["subject"][:60]
        if dry_run:
            print(f"[gmail_poller] [dry-run] {label}: would queue {subject_short!r}")
            print(f"             from={full['from_addr']}")
            print(f"             body_len={len(full['body'])} chars")
            continue

        # Ingress audit log — email arrives from gmail_poller, not a user channel
        try:
            from gateway.ingress import InboundMessage as _IM, Channel as _Ch, receive as _ingress_receive
            _ingress_receive(_IM(
                raw_text=f"email:{label}:{subject_short}",
                sender_id=full.get("from_addr", "unknown"),
                channel=_Ch.EMAIL,
                channel_id=group_id,
                message_id=msg["id"],
                source="gmail_poller",
            ))
        except Exception:
            pass  # non-fatal

        item_id = write_item(
            intent="gmail_label",
            raw_message=f"email:{label}:{subject_short}",
            sender="gmail_poller",
            channel_id=group_id,
            source="telegram",
            payload={
                "label": label,
                "message_id": msg["id"],
                "member_id": label_cfg.get("member_id", ""),
                "from_addr": full["from_addr"],
                "subject": full["subject"],
                "date": full["date"],
                "body": full["body"],   # already capped to 3000 chars in fetch_full()
            },
        )
        update_status(item_id, "confirmed")   # executor picks up immediately
        state.setdefault("seen_ids", []).append(msg["id"])
        print(f"[gmail_poller] {label}: queued [{item_id[:8]}] {subject_short!r}")

    # Cap seen list at 200 entries to prevent unbounded growth
    state["seen_ids"] = state.get("seen_ids", [])[-200:]
    state["last_poll"] = _now()
    if not dry_run:
        _save_state(label, state)


def main(dry_run: bool = False, reprocess: str = "") -> None:
    labels = aaka_config.gmail_labels()
    if not labels:
        print("[gmail_poller] no gmail_labels configured in aaka.yaml — exiting")
        return

    # --reprocess: clear seen_ids for the named label before ingesting
    if reprocess:
        matched = any(lc["label"] == reprocess for lc in labels)
        if not matched:
            print(f"[gmail_poller] --reprocess: label {reprocess!r} not found in aaka.yaml")
            return
        path = _state_path(reprocess)
        if path.exists():
            state = json.loads(path.read_text())
            state["seen_ids"] = []
            if not dry_run:
                path.write_text(json.dumps(state, indent=2))
            print(f"[gmail_poller] --reprocess: cleared seen_ids for {reprocess!r}"
                  + (" (dry-run)" if dry_run else ""))
        else:
            print(f"[gmail_poller] --reprocess: no state file for {reprocess!r} — will bootstrap on first run")

    # Group labels by member_id so we only authenticate once per account
    by_member: dict[str, list] = {}
    for lc in labels:
        # When --reprocess is set, only process that label
        if reprocess and lc["label"] != reprocess:
            continue
        mid = lc.get("member_id", "")
        by_member.setdefault(mid, []).append(lc)

    for member_id, member_labels in by_member.items():
        try:
            svc = get_gmail_service(member_id)
        except Exception as exc:
            print(f"[gmail_poller] Gmail auth failed for member={member_id!r} — {exc}")
            continue
        for label_cfg in member_labels:
            _ingest_label(svc, label_cfg, dry_run)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reprocess", metavar="LABEL", default="",
                   help="Clear seen state for LABEL and re-queue all unread+labeled emails")
    args = p.parse_args()
    main(dry_run=args.dry_run, reprocess=args.reprocess)
