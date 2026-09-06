"""
skills/mail/gmail_attachment_filer.py — Executor skill: file Gmail attachments.

Triggered by a gmail_attachment_file queue item (written by _exec_gmail_label when
the label has a matching tag route with gmail_label: "Label/Name" in references.yaml).

Payload keys:
    message_id  — Gmail message ID
    member_id   — which account to authenticate (matches aaka.yaml member id)
    label       — Gmail label name (for logging)
    tag_name    — references.yaml route key, e.g. "payslips"
    dest_path   — vault path, e.g. "finance/payslips"
    dest_owner  — member id who owns the vault, e.g. "alex"

Each attachment is:
  1. Saved to $AAKA_CONFIG_DIR/data/staging/gmail_att/<message_id>/
  2. A drop_file queue item is written + confirmed for executor pickup

Returns: {filed: int, skipped: int, attachments: [...]}
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))

import aaka_config


def execute(payload: dict) -> dict:
    message_id = payload.get("message_id", "")
    member_id  = payload.get("member_id", "")
    label      = payload.get("label", "")
    tag_name   = payload.get("tag_name", "")
    dest_path  = payload.get("dest_path", "")
    dest_owner = payload.get("dest_owner", "")

    if not message_id:
        raise ValueError("gmail_attachment_filer: missing message_id in payload")

    from skills.mail.gmail import get_gmail_service, fetch_attachments
    svc = get_gmail_service(member_id)
    attachments = fetch_attachments(svc, message_id)

    if not attachments:
        return {"filed": 0, "skipped": 0, "attachments": [], "note": "no attachments"}

    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR") or aaka_config.CONFIG_DIR)
    staging_root = config_dir / "data" / "staging" / "gmail_att" / message_id
    staging_root.mkdir(parents=True, exist_ok=True)

    from aaka_queue.queue import write_item, update_status

    filed = 0
    skipped = 0
    filed_names: list[str] = []

    for att in attachments:
        filename = att["filename"]
        data     = att["data"]
        if not data:
            skipped += 1
            continue

        # Sanitize filename: replace path separators
        safe_name = Path(filename).name
        dest_file = staging_root / safe_name
        # Avoid overwrite: suffix duplicates
        if dest_file.exists():
            stem, ext = os.path.splitext(safe_name)
            dest_file = staging_root / f"{stem}_{message_id[:8]}{ext}"
        dest_file.write_bytes(data)

        # Staging path relative to $AAKA_CONFIG_DIR/data/ (drop_file convention)
        staging_rel = str(dest_file.relative_to(config_dir / "data"))

        # Build drop_file payload via Smart Drop path
        drop_payload: dict = {
            "media_staging_path": staging_rel,
            "original_filename":  safe_name,
            "mime_type":          att["mime_type"],
            "file_size_bytes":    att["size"],
            "caption":            f"via gmail:{label}",
            "namespace":          dest_owner or member_id or "user",
            "source":             "gmail_attachment_filer",
            "actor":              dest_owner or member_id or "user",
            "hash_tags":          [tag_name] if tag_name else [],
            "skip_compress":      False,
        }

        item_id = write_item(
            intent="drop_file",
            raw_message=f"gmail_attachment:{label}:{safe_name}",
            sender="gmail_attachment_filer",
            channel_id="",
            source="telegram",
            payload=drop_payload,
        )
        update_status(item_id, "confirmed")
        filed += 1
        filed_names.append(safe_name)
        print(f"[gmail_filer] queued drop_file [{item_id[:8]}] {safe_name} → {dest_path}")

    return {
        "filed": filed,
        "skipped": skipped,
        "attachments": filed_names,
        "label": label,
        "tag_name": tag_name,
        "dest_path": dest_path,
    }
