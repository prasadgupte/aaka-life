#!/usr/bin/env python3
"""
tools/file_utils.py — Filename sanitization, extension validation, size checks.

Used by drop_note and drop_file skills for secure file handling.
"""

import os
import re
from datetime import date
from pathlib import Path

_ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp",
    ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".md",
    ".mp4", ".mp3", ".ogg", ".wav", ".m4a",
}

_MAX_STEM_LENGTH = 80
_DEFAULT_MAX_MB = 50


def sanitize_filename(name: str) -> str:
    """Sanitize a filename for safe filesystem storage.

    - Lowercases
    - Replaces spaces/unsafe chars with hyphens
    - Rejects path traversal (../, leading /)
    - Truncates stem to 80 chars
    - Collapses repeated hyphens
    """
    # Reject null bytes
    name = name.replace("\x00", "")
    # Take only the basename (strip any directory components)
    name = os.path.basename(name)
    # Split stem and extension
    stem, ext = os.path.splitext(name)
    # Lowercase
    stem = stem.lower()
    ext = ext.lower()
    # Replace spaces and unsafe chars with hyphens
    stem = re.sub(r'[^a-z0-9._()-]+', '-', stem)
    # Collapse repeated hyphens
    stem = re.sub(r'-{2,}', '-', stem)
    # Strip leading/trailing hyphens
    stem = stem.strip('-')
    # Truncate
    if len(stem) > _MAX_STEM_LENGTH:
        stem = stem[:_MAX_STEM_LENGTH].rstrip('-')
    # Fallback for empty stem
    if not stem:
        stem = "file"
    return stem + ext


def validate_extension(ext: str) -> bool:
    """Check if a file extension is in the allowlist."""
    return ext.lower() in _ALLOWED_EXTENSIONS


def validate_size(path: Path, max_mb: int = _DEFAULT_MAX_MB) -> bool:
    """Check if a file is within the size limit."""
    try:
        size = path.stat().st_size
        return size <= max_mb * 1024 * 1024
    except OSError:
        return False


def generate_drop_filename(tags: list[str], original: str, ext: str = "",
                           compressed: bool = False) -> str:
    """Generate a dated, tagged filename for a dropped file.

    Format: YYMMDD_tag1-tag2_sanitized-original[_c].ext

    Args:
        tags: List of tags (may be empty)
        original: Original filename (stem only or with extension)
        ext: Override extension (if empty, taken from original)
        compressed: If True, appends _c before the extension
    """
    today = date.today().strftime("%y%m%d")

    if not ext:
        _, ext = os.path.splitext(original)
    ext = ext.lower()

    # Sanitize the original stem
    orig_stem = os.path.splitext(original)[0] if ext else original
    orig_clean = sanitize_filename(orig_stem + ".tmp").replace(".tmp", "")

    parts = [today]
    if tags:
        tag_str = "-".join(t.lower() for t in tags[:5])  # max 5 tags in filename
        parts.append(tag_str)
    if orig_clean and orig_clean != "file":
        parts.append(orig_clean)

    stem = "_".join(parts)
    if compressed:
        stem += "_c"
    return stem + ext


def generate_smart_name(custom_name: str, ext: str, compressed: bool = False) -> str:
    """Generate a filename from a quoted custom name.

    Format: sanitized-custom-name[_c].ext

    Used when the user provides a quoted string in the drop caption, e.g.:
      f ari health "260507-doctor-update"  →  260507-doctor-update[_c].pdf
    """
    clean = sanitize_filename(custom_name + ".tmp").replace(".tmp", "")
    if not clean:
        clean = "file"
    ext = ext.lower() if ext else ""
    if compressed:
        return clean + "_c" + ext
    return clean + ext
