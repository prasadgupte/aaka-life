"""
skills/drop/format.py — User-facing message renderers for /drop staging replies.

Owns presentation only. Logic / routing lives in skills/drop/drop_file.py and
tools/inbox_router.py. The sensor router calls these renderers when it acks a
staged file, so the user sees the target folder before the executor moves it.
"""


def _name_chip(safe_name: str, size_kb: int) -> str:
    return f"📎 {safe_name} ({size_kb} KB)"


def _human_size(n: int) -> str:
    """Pretty-print a byte count: 1234 → 1.2 KB, 8400000 → 8.4 MB."""
    if n is None or n <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB")
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            if u == "B":
                return f"{int(v)} B"
            return f"{v:.1f} {u}"
        v /= 1024
    return f"{v:.1f} GB"


def render_drop_success(
    orig_filename: str,
    filed_rel: str,
    renamed_to: "str | None" = None,
    compressed: bool = False,
    original_size_bytes: "int | None" = None,
    new_size_bytes: "int | None" = None,
    keep_hash: str = "",
    unknown_tags: "list[str] | None" = None,
    routed: bool = True,
    error_hint: "str | None" = None,
    learned: "list[str] | None" = None,
) -> str:
    """Reply for a completed drop_file. Mentions rename, compression delta,
    and a tap-to-copy `keep #hash` hint when the original is recoverable.
    """
    lines: list[str] = []
    renamed = bool(renamed_to and renamed_to != orig_filename)
    rename_marker = "  ✏️" if renamed else ""
    lines.append(f"📎 Filed: {orig_filename} → {filed_rel}{rename_marker}")

    if compressed and original_size_bytes and new_size_bytes:
        ratio_pct = (1 - new_size_bytes / original_size_bytes) * 100
        lines.append(
            f"🗜 Compressed: {_human_size(original_size_bytes)} → {_human_size(new_size_bytes)}"
            f" (-{ratio_pct:.0f}%)"
        )
        if keep_hash:
            lines.append(f"↪ keep original? `keep #{keep_hash}`")

    if error_hint:
        lines.append(f"⚠️ {error_hint}")
    elif unknown_tags and not routed:
        lines.append(f"⚠️ Tags not recognized: {', '.join(unknown_tags)}")

    if learned:
        lines.append("🧠 Learned: " + ", ".join(f"#{t}" for t in learned)
                     + " — next time no area needed")

    return "\n".join(lines)


def render_keep_result(result: dict) -> str:
    """Reply for the keep_original handler."""
    status = result.get("status")
    keep_hash = result.get("keep_hash", "")
    if status == "ok":
        msg = f"↩️ Restored original: {result.get('original_filename', 'file')} → {result.get('file', '')}"
        if not result.get("removed_compressed"):
            msg += "\n(compressed copy left in place)"
        return msg
    err = result.get("error_hint") or "could not restore"
    return f"⚠️ keep #{keep_hash}: {err}"


def _tag_chip(tags: "list[str]") -> str:
    return " ".join(f"#{t}" for t in tags) if tags else ""


def render_smart_drop_preview(
    safe_name: str,
    actor: str,
    area: "str | None",
    hash_tags: "list[str]",
    custom_name: "str | None",
    skip_compress: bool,
    hash_line: str,
) -> str:
    """Smart Drop ack — explicit destination already known from caption parse."""
    parts = [actor]
    if area:
        parts.append(area)
    parts.extend(hash_tags)
    dest = "/".join(parts) + "/"
    name_hint = f' as "{custom_name}"' if custom_name else ""
    compress_hint = "" if skip_compress else " (compress 150dpi)"
    return f"📎 {safe_name}{name_hint} → {dest}{compress_hint}{hash_line}"


def render_legacy_drop_preview(
    safe_name: str,
    size_kb: int,
    tags: "list[str]",
    destination: dict,
    hash_line: str,
) -> str:
    """Legacy / hashtag-only drop — resolves via references.yaml.

    `destination` is the dict returned by inbox_router.preview_destination().
    Always shows the target folder; appends a one-line hint when the
    resolution is partial (entity matched but no action) or unknown.
    """
    head = _name_chip(safe_name, size_kb)
    tag_str = _tag_chip(tags)
    if tag_str:
        head += f" {tag_str}"

    dest_rel = (destination.get("dest_rel") or "00-Inbox").rstrip("/")
    owner = destination.get("vault_owner") or ""
    if owner == "shared":
        dest_display = f"~/{dest_rel}/"
    elif owner:
        dest_display = f"{owner}/{dest_rel}/"
    else:
        dest_display = f"{dest_rel}/"

    arrow = f"\n→ {dest_display}"

    if destination.get("needs_action"):
        label = destination.get("entity_label")
        ent_str = f" → {label}" if label else ""
        arrow += f"  (matched entity{ent_str}; add an action tag like #expense / #invoice for a final folder)"
    elif destination.get("unknown"):
        unk = " ".join(f"#{t}" for t in destination["unknown"])
        arrow += f"  (unknown: {unk})"

    return f"{head}{arrow}{hash_line}"


def render_staged_ack(
    safe_name: str,
    size_kb: int,
    tags: "list[str]",
    destination: "dict | None",
    hash_line: str,
) -> str:
    """Used by the drop-ask confirmation path (user replied with tags).

    Same shape as render_legacy_drop_preview but prefixed with "File staged —"
    to distinguish from the immediate /drop ack.
    """
    head = f"📎 File staged — {safe_name} ({size_kb} KB)"
    tag_str = _tag_chip(tags)
    if tag_str:
        head += f" {tag_str}"
    if destination is None:
        return f"{head}{hash_line}"

    dest_rel = (destination.get("dest_rel") or "00-Inbox").rstrip("/")
    owner = destination.get("vault_owner") or ""
    if owner == "shared":
        dest_display = f"~/{dest_rel}/"
    elif owner:
        dest_display = f"{owner}/{dest_rel}/"
    else:
        dest_display = f"{dest_rel}/"
    arrow = f"\n→ {dest_display}"
    if destination.get("needs_action"):
        label = destination.get("entity_label")
        ent_str = f" → {label}" if label else ""
        arrow += f"  (matched entity{ent_str}; add an action tag for a final folder)"
    elif destination.get("unknown"):
        unk = " ".join(f"#{t}" for t in destination["unknown"])
        arrow += f"  (unknown: {unk})"
    return f"{head}{arrow}{hash_line}"
