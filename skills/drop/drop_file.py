"""
skills/drop/drop_file.py — Executor handler for drop_file intent.

Smart Drop path (payload has `actor`):
  - Resolves destination via inbox_router.resolve_smart_drop()
  - Auto-compresses PDFs at 150dpi/q80 (unless skip_compress)
  - Moves originals to recycle_bin/
  - Uses quoted custom_name for naming when provided
  - Appends _c suffix on compressed files
  - Writes audit log to data/staging/drop_log.tsv

Legacy path (no `actor` key):
  - Resolves via inbox_router.route() + references.yaml
  - Falls back to 00-Inbox/<namespace>/
  - Creates companion .meta.md
"""

import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aaka_config
from tools.file_utils import generate_drop_filename, generate_smart_name, sanitize_filename


def _recycle(file_path: Path, config_dir: Path) -> Path:
    """Move a file to recycle_bin/YYYY-MM-DD/ for later manual cleanup.

    Returns the final destination path (after any collision-rename) so
    callers can record it in the keep-index for `keep #hash`.
    """
    today_str = date.today().isoformat()
    bin_dir = config_dir / "data" / "recycle_bin" / today_str
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / file_path.name
    # Avoid name collision
    if dest.exists():
        stem, ext = os.path.splitext(file_path.name)
        for i in range(2, 1000):
            candidate = bin_dir / f"{stem}-{i}{ext}"
            if not candidate.exists():
                dest = candidate
                break
    shutil.move(str(file_path), str(dest))
    return dest


def _cleanup_recycle_bin(config_dir: Path, max_age_years: int = 3) -> int:
    """Delete recycle_bin date-folders older than max_age_years. Called on every drop."""
    bin_root = config_dir / "data" / "recycle_bin"
    if not bin_root.exists():
        return 0
    cutoff = date.today() - timedelta(days=max_age_years * 365)
    deleted = 0
    for d in bin_root.iterdir():
        if d.is_dir():
            try:
                folder_date = date.fromisoformat(d.name)
                if folder_date < cutoff:
                    shutil.rmtree(str(d))
                    deleted += 1
            except ValueError:
                continue
    return deleted


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tiff", ".tif", ".bmp"}
# Images smaller than this are not worth compressing — already small enough.
_IMAGE_COMPRESS_MIN_BYTES = 500 * 1024
# Cap longest side for resize. Phone-camera images are 4032px+; 2400 is plenty
# for vault-archive use and cuts file size 4-6×.
_IMAGE_MAX_DIM = 2400
_IMAGE_JPEG_QUALITY = 82


def _compress_pdf(staged: Path, config_dir: Path, skip: bool) -> dict:
    """Compress a PDF at 150dpi/q80, move original to recycle_bin.

    Returns {path, compressed, recycled_path, original_size, new_size}.
    On skip / non-pdf / failure: compressed=False, recycled_path=None,
    sizes still populated where possible.
    """
    try:
        original_size = staged.stat().st_size
    except OSError:
        original_size = 0
    if skip or staged.suffix.lower() != ".pdf":
        return {"path": staged, "compressed": False, "recycled_path": None,
                "original_size": original_size, "new_size": original_size}
    try:
        from tools.pdf_tool import compress as _compress
        comp_name = staged.stem + "_c_tmp.pdf"
        comp_path = staged.parent / comp_name
        result = _compress(str(staged), str(comp_path), quality=80, dpi=150)
        comp_out = Path(result["output"])
        if comp_out.exists() and comp_out.stat().st_size > 0:
            new_size = comp_out.stat().st_size
            recycled = _recycle(staged, config_dir)
            return {"path": comp_out, "compressed": True, "recycled_path": recycled,
                    "original_size": original_size, "new_size": new_size}
    except Exception:
        pass
    return {"path": staged, "compressed": False, "recycled_path": None,
            "original_size": original_size, "new_size": original_size}


def _compress_image(staged: Path, config_dir: Path, skip: bool) -> dict:
    """Downscale + re-encode large images.

    Strategy: resize so the longest side is <= _IMAGE_MAX_DIM, then encode JPEG
    at quality 82 (PNG/WebP/HEIC all flatten to JPEG for vault use — fine for
    receipts, scans, photo dumps). Original goes to recycle_bin/.

    Skip when: skip=True, ext isn't an image, file is already small, or PIL
    is unavailable / fails. Also skip if the compressed output isn't smaller
    than the original (PNG screenshots of text often grow as JPEG).

    Returns {path, compressed, recycled_path, original_size, new_size}.
    """
    ext = staged.suffix.lower()
    try:
        original_size = staged.stat().st_size
    except OSError:
        original_size = 0
    no_op = {"path": staged, "compressed": False, "recycled_path": None,
             "original_size": original_size, "new_size": original_size}

    if skip or ext not in _IMAGE_EXTS or original_size == 0:
        return no_op
    if original_size < _IMAGE_COMPRESS_MIN_BYTES:
        return no_op
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return no_op
    try:
        comp_path = staged.parent / (staged.stem + "_c_tmp.jpg")
        with Image.open(str(staged)) as img:
            # Apply EXIF orientation so the saved image is upright
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            w, h = img.size
            longest = max(w, h)
            if longest > _IMAGE_MAX_DIM:
                scale = _IMAGE_MAX_DIM / longest
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            img.save(str(comp_path), "JPEG",
                     quality=_IMAGE_JPEG_QUALITY, optimize=True, progressive=True)
        new_size = comp_path.stat().st_size if comp_path.exists() else 0
        if new_size == 0 or new_size >= original_size:
            try:
                comp_path.unlink()
            except OSError:
                pass
            return no_op
        recycled = _recycle(staged, config_dir)
        return {"path": comp_path, "compressed": True, "recycled_path": recycled,
                "original_size": original_size, "new_size": new_size}
    except Exception:
        return no_op


def _compress(staged: Path, config_dir: Path, skip: bool) -> dict:
    """Dispatch to the right compressor for the file's type. No-op otherwise.

    Returns {path, compressed, recycled_path, original_size, new_size}.
    """
    ext = staged.suffix.lower()
    if ext == ".pdf":
        return _compress_pdf(staged, config_dir, skip)
    if ext in _IMAGE_EXTS:
        return _compress_image(staged, config_dir, skip)
    try:
        original_size = staged.stat().st_size
    except OSError:
        original_size = 0
    return {"path": staged, "compressed": False, "recycled_path": None,
            "original_size": original_size, "new_size": original_size}


def _write_keep_index(config_dir: Path, keep_hash: str, entry: dict) -> None:
    """Persist a keep-index entry so `keep #hash` can restore the original.

    File: $AAKA_CONFIG_DIR/data/staging/keep_index.json (a small dict).
    Stays on the Mac (executor) — the recycle_bin it points into is local.
    """
    import json
    if not keep_hash:
        return
    idx_path = config_dir / "data" / "staging" / "keep_index.json"
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing[keep_hash] = entry
    # Cap size at 500 entries (oldest dropped first) so the index doesn't grow forever
    if len(existing) > 500:
        for stale in list(existing.keys())[:-500]:
            existing.pop(stale, None)
    idx_path.write_text(json.dumps(existing, indent=2))


def read_keep_index(config_dir: Path) -> dict:
    """Read the keep-index dict. Used by the keep_original handler."""
    import json
    idx_path = config_dir / "data" / "staging" / "keep_index.json"
    if not idx_path.exists():
        return {}
    try:
        return json.loads(idx_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_keep_index(config_dir: Path, data: dict) -> None:
    """Replace the keep-index dict (used by the keep_original handler)."""
    import json
    idx_path = config_dir / "data" / "staging" / "keep_index.json"
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(data, indent=2))


def _audit_log(config_dir: Path, **kwargs) -> None:
    """Append a row to data/staging/drop_log.tsv."""
    log_path = config_dir / "data" / "staging" / "drop_log.tsv"
    header = "timestamp\tincoming_filename\toriginal_msg\tfile_count\ttarget_filename\ttarget_folder\tactor\tcompressed\tsize_kb\n"
    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(header)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = "\t".join([
        now,
        str(kwargs.get("incoming", "")),
        str(kwargs.get("msg", "")).replace("\t", " ").replace("\n", " "),
        str(kwargs.get("file_count", 1)),
        str(kwargs.get("target_name", "")),
        str(kwargs.get("target_folder", "")),
        str(kwargs.get("actor", "")),
        str(kwargs.get("compressed", False)),
        str(kwargs.get("size_kb", 0)),
    ])
    with open(log_path, "a") as f:
        f.write(row + "\n")


def _append_context_entry(dest_dir: Path, filename: str, description: str,
                          queue_hash: str, tags: list) -> None:
    """Append a 📎 file entry to _context.md in the target folder.

    Format: - YYYY-MM-DD 📎 filename | description | #hash

    All sources (notes, file drops, future email) share this file as a
    chronological running log. Creates _context.md if it doesn't exist.
    """
    from datetime import date
    context_md = dest_dir / "_context.md"
    desc_part = description if description else ""
    hash_part = f" | #{queue_hash}" if queue_hash else ""
    today = date.today().isoformat()
    line = f"- {today} 📎 {filename} | {desc_part}{hash_part}\n"
    is_new = not context_md.exists()
    with open(context_md, "a") as f:
        if is_new:
            tag_str = ", ".join(tags) if tags else ""
            f.write(f"---\ntags: [{tag_str}]\ntype: context\n---\n\n")
        f.write(line)


def _append_files_md(dest_dir: Path, filename: str, description: str,
                     queue_hash: str, tags: list) -> None:
    """Append an entry to _context.md (unified context file).

    Delegates to _append_context_entry. The old files.md name is kept as an
    alias so existing call sites work without change.
    """
    _append_context_entry(dest_dir, filename, description, queue_hash, tags)


def describe() -> str:
    """Help text for /drop (f help)."""
    # Dynamic tag ideas from existing note topics
    notes_dir = aaka_config.DATA_DIR / "notes"
    topics = sorted(p.stem for p in notes_dir.glob("*.md")) if notes_dir.exists() else []
    tag_line = "  tags: " + " ".join(f"#{t}" for t in topics) if topics else ""

    return (
        "📎 *Drop* — file to vault\n\n"
        "*Smart Drop*\n"
        "  f [member] [area] [#tag] [\"name\"]. description\n"
        "  e.g. f ari health #ortho \"260507-doc\". follow up visit\n"
        "  e.g. f ari tax. Finanzamt 2025 annual\n\n"
        "*Routing*\n"
        "  member: ari / rumi / alex / tsu / kiran\n"
        "  area:   scanned from vault (health, career, finance…)\n"
        "  #tag → creates subfolder + auto-learns for next time\n"
        "  tax, insurance, visa… → direct route (no area needed)\n"
        "  \".\" separates routing from description\n"
        "  \"name\" → filename (1 file) or subfolder (multiple)\n\n"
        "*PDF*  auto-compressed 150dpi; add keep/og to skip\n"
        "*Images*  large JPG/PNG/HEIC etc. downsized to ≤2400px JPEG q82\n\n"
        "*Legacy*  f flo172 expense → routed via references.yaml\n\n"
        "*Undo*  /undo #hash · cancel/move-back the filed drop\n"
        "*Keep original*  `keep #hash` (shown after a compressed drop)\n"
        + (f"\n{tag_line}" if tag_line else "")
    )


_VAULT_TOPLEVEL = {"00-Inbox", "01-Projects", "03-Resources",
                   "04-Notes", "90-Archives", "95-Backup", "99-System"}


def _resolve_dest_dir(vault: Path, file_to: str) -> Path:
    """Convert a file_to template result to an absolute vault path."""
    return vault / file_to


def execute(payload: dict) -> dict:
    """Move staged file to vault.

    Smart Drop path (payload has non-None `actor`):
      - Resolves via inbox_router.resolve_smart_drop()
      - Auto-compresses PDFs (150dpi/q80) unless skip_compress
      - Original moved to recycle_bin/ before compression
      - Compressed files get _c suffix
      - Quoted custom_name used for naming when provided
      - Appends audit row to drop_log.tsv

    Legacy path (actor is None):
      - Resolves via inbox_router.route() + references.yaml
      - Falls back to 00-Inbox/<namespace>/
      - Creates companion .meta.md

    Common payload keys:
        tags: list[str]              — routing / naming tags
        media_staging_path: str      — relative to $AAKA_CONFIG_DIR/data/
        mime_type: str
        original_filename: str
        file_size_bytes: int
        caption: str
        namespace: str               — member id (sender's)
        message_id: str|None
        source: str
        actor: str|None              — resolved member id (Smart Drop)
        area: str|None               — known area folder
        hash_tags: list[str]         — subfolder-create tags
        custom_name: str|None        — quoted name
        skip_compress: bool
        file_count: int
    """
    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/Users/Shared/aaka-repo-config"))

    # Opportunistic recycle bin cleanup (fast: just checks folder names vs date)
    try:
        _cleanup_recycle_bin(config_dir)
    except Exception:
        pass

    staging_rel = payload.get("media_staging_path", "")
    if not staging_rel:
        raise ValueError("No staging path in payload")

    staged = config_dir / "data" / staging_rel
    if not staged.exists():
        raise FileNotFoundError(
            f"Staged file not yet synced: {staging_rel} "
            "(will retry on next executor cycle)"
        )

    tags = payload.get("tags", [])
    namespace = payload.get("namespace", "user")
    source = payload.get("source", "telegram")
    caption = payload.get("caption", "").strip()
    original = payload.get("original_filename", staged.name)
    actor = payload.get("actor")  # None → legacy path

    def _cleanup_staged(path: Path) -> None:
        try:
            path.unlink()
            if path.parent != config_dir / "data" / "staging":
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
        except OSError:
            pass

    skip_compress = bool(payload.get("skip_compress"))
    queue_id = payload.get("_queue_id") or ""
    keep_hash = queue_id[:8] if queue_id else ""

    def _record_keep(was_compressed: bool, recycled_path: "Path | None",
                     vault_root: Path, dest: Path, namespace_or_shared: str) -> None:
        if not (was_compressed and recycled_path and keep_hash):
            return
        try:
            rel_recycled = str(recycled_path.relative_to(config_dir))
        except ValueError:
            rel_recycled = str(recycled_path)
        try:
            rel_vault_dest = str(dest.relative_to(vault_root))
        except ValueError:
            rel_vault_dest = str(dest)
        _write_keep_index(config_dir, keep_hash, {
            "recycled_path": rel_recycled,            # rel to AAKA_CONFIG_DIR
            "vault_owner": namespace_or_shared,        # member id or 'shared'
            "vault_dest": rel_vault_dest,              # rel to vault_root — the compressed file
            "original_filename": original,
            "stored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    # ── Smart Drop path ────────────────────────────────────────────────────────
    if actor:
        from tools.inbox_router import resolve_smart_drop
        area = payload.get("area")
        hash_tags = payload.get("hash_tags") or []
        custom_name = payload.get("custom_name")
        file_count = int(payload.get("file_count") or 1)
        description = payload.get("description", "")

        smart = resolve_smart_drop(actor, area, hash_tags, custom_name, file_count,
                                   tags=tags)
        error_hint = smart.get("error") if smart.get("error") else None
        learned = smart.get("learned") or []

        vault_root = smart["vault_root"] or aaka_config.vault_path_for(namespace) or Path(config_dir / "data")
        dest_dir = vault_root / smart["dest_rel"]
        dest_dir.mkdir(parents=True, exist_ok=True)

        # PDF or image compression (both dispatch via _compress)
        comp = _compress(staged, config_dir, skip_compress)
        staged = comp["path"]
        was_compressed = comp["compressed"]
        original_size = comp["original_size"]
        new_size = comp["new_size"]
        recycled_path = comp["recycled_path"]

        # Filename generation
        _, ext = os.path.splitext(original)
        # _compress may have switched ext (e.g. .png → .jpg); honor staged path's ext
        if was_compressed:
            ext = staged.suffix or ext
        if custom_name and file_count == 1:
            new_name = generate_smart_name(custom_name, ext, compressed=was_compressed)
        else:
            new_name = generate_drop_filename(tags or hash_tags or ([area] if area else []),
                                              original, compressed=was_compressed)
            if was_compressed and staged.suffix and not new_name.endswith(staged.suffix):
                # generate_drop_filename uses the original extension; for image
                # compression we converted to .jpg so swap the trailing ext.
                stem, _ = os.path.splitext(new_name)
                new_name = stem + staged.suffix

        dest = dest_dir / new_name
        if dest.exists():
            stem, fext = os.path.splitext(new_name)
            for i in range(2, 1000):
                candidate = dest_dir / f"{stem}-{i}{fext}"
                if not candidate.exists():
                    dest = candidate
                    break

        shutil.copy2(str(staged), str(dest))
        _cleanup_staged(staged)

        rel_path = str(dest.relative_to(vault_root))
        size_kb = (dest.stat().st_size // 1024) if dest.exists() else 0

        queue_hash = payload.get("message_id", "")[:8] if payload.get("message_id") else ""
        all_tags = list(tags) + list(hash_tags)
        _append_files_md(dest_dir, dest.name, description, queue_hash, all_tags)

        _audit_log(
            config_dir, incoming=original, msg=caption, file_count=file_count,
            target_name=dest.name, target_folder=rel_path, actor=actor,
            compressed=was_compressed, size_kb=size_kb,
        )

        vault_owner = "shared" if vault_root == aaka_config.SHARED_VAULT_PATH else (actor or namespace)
        _record_keep(was_compressed, recycled_path, vault_root, dest, vault_owner)

        return {
            "status": "ok",
            "file": rel_path,
            "tags": tags,
            "original_filename": original,
            "renamed_to": dest.name,
            "message_id": payload.get("message_id"),
            "routed": True,
            "unknown_tags": [],
            "compressed": was_compressed,
            "original_size_bytes": original_size,
            "new_size_bytes": new_size,
            "keep_hash": keep_hash if was_compressed else "",
            "error_hint": error_hint,
            "learned": learned,
        }

    # ── Legacy path (no actor) ─────────────────────────────────────────────────
    vault = aaka_config.vault_path_for(namespace)
    shared_vault = aaka_config.SHARED_VAULT_PATH

    from tools.inbox_router import route as inbox_route
    route_result = inbox_route(tags, namespace, vault=vault, actor=namespace)
    outcomes = route_result.get("outcomes", [])
    unknown_tags = route_result.get("unknown", [])

    file_to = None
    vault_root = vault
    is_shared = False
    for o in outcomes:
        ft = (o.get("file_to") or "").strip().rstrip("/")
        if ft:
            file_to = ft
            act_def = o.get("action_def") or {}
            if act_def.get("vault_root") == "shared":
                vault_root = shared_vault
                is_shared = True
            break

    if file_to:
        dest_dir = _resolve_dest_dir(vault_root, file_to)
    else:
        dest_dir = vault / "00-Inbox"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Compress before naming so the _c suffix lands on the right file
    comp = _compress(staged, config_dir, skip_compress)
    staged = comp["path"]
    was_compressed = comp["compressed"]
    original_size = comp["original_size"]
    new_size = comp["new_size"]
    recycled_path = comp["recycled_path"]

    new_name = generate_drop_filename(tags, original, compressed=was_compressed)
    if was_compressed and staged.suffix and not new_name.endswith(staged.suffix):
        stem, _ = os.path.splitext(new_name)
        new_name = stem + staged.suffix
    dest = dest_dir / new_name

    if dest.exists():
        stem, ext = os.path.splitext(new_name)
        for i in range(2, 100):
            candidate = dest_dir / f"{stem}-{i}{ext}"
            if not candidate.exists():
                dest = candidate
                break

    shutil.copy2(str(staged), str(dest))

    description = payload.get("description", "")
    queue_hash = payload.get("message_id", "")[:8] if payload.get("message_id") else ""
    _append_files_md(dest_dir, dest.name, description, queue_hash, tags)

    _cleanup_staged(staged)

    rel_path = str(dest.relative_to(vault_root))
    vault_owner = "shared" if is_shared else namespace
    _record_keep(was_compressed, recycled_path, vault_root, dest, vault_owner)

    return {
        "status": "ok",
        "file": rel_path,
        "tags": tags,
        "original_filename": original,
        "renamed_to": dest.name,
        "message_id": payload.get("message_id"),
        "routed": bool(file_to),
        "unknown_tags": unknown_tags,
        "compressed": was_compressed,
        "original_size_bytes": original_size,
        "new_size_bytes": new_size,
        "keep_hash": keep_hash if was_compressed else "",
    }
