"""
tools/pdf_page_spec.py — Page specification parser for PDF split and delete operations.

Syntax (all page numbers are 1-indexed in input; outputs are 0-indexed):

  "2s"      stride — every N pages
  "3-5"     range  — pages 3 through 5 inclusive
  "1,5,9"   list   — explicit pages (for delete) or split-points (for split)

For split, a list like "1,5,9" on a 10-page doc means:
  Split at the START of pages 1, 5, 9 → chunks [1-4], [5-8], [9-10].
  The first value is always 1 (start), so leading 1 is optional but valid.

For delete, a list like "1,5,9" means remove pages 1, 5, and 9.
"""

import re


def _parse_stride(spec: str, total_pages: int) -> int:
    """Return the stride N from a spec like '2s'. Raises ValueError on bad input."""
    m = re.fullmatch(r"(\d+)s", spec.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"Invalid stride spec: {spec!r}")
    n = int(m.group(1))
    if n < 1:
        raise ValueError(f"Stride must be >= 1, got {n}")
    return n


def _parse_range(spec: str, total_pages: int) -> tuple[int, int]:
    """Return (start, end) as 0-indexed from a spec like '3-5'. Raises ValueError."""
    m = re.fullmatch(r"(\d+)-(\d+)", spec.strip())
    if not m:
        raise ValueError(f"Invalid range spec: {spec!r}")
    start, end = int(m.group(1)), int(m.group(2))
    if start < 1 or end < start:
        raise ValueError(f"Range must be start <= end, both >= 1; got {spec!r}")
    if start > total_pages or end > total_pages:
        raise ValueError(f"Range {spec!r} out of bounds for {total_pages}-page document")
    return start - 1, end - 1  # convert to 0-indexed


def _parse_list(spec: str, total_pages: int) -> list[int]:
    """Return list of 0-indexed page numbers from a comma-separated spec like '1,5,9'."""
    parts = [p.strip() for p in spec.split(",")]
    pages = []
    for p in parts:
        if not p.isdigit():
            raise ValueError(f"Expected integer page number, got {p!r}")
        n = int(p)
        if n < 1 or n > total_pages:
            raise ValueError(f"Page {n} out of bounds for {total_pages}-page document")
        pages.append(n - 1)  # convert to 0-indexed
    return pages


def _detect_mode(spec: str) -> str:
    """Detect spec mode: 'stride', 'range', or 'list'."""
    s = spec.strip()
    if re.fullmatch(r"\d+s", s, re.IGNORECASE):
        return "stride"
    if re.fullmatch(r"\d+-\d+", s):
        return "range"
    if re.fullmatch(r"\d+(,\s*\d+)*", s):
        return "list"
    raise ValueError(
        f"Unrecognised page spec {spec!r}. "
        "Use '2s' (every 2 pages), '3-5' (range), or '1,5,9' (list)."
    )


# ── Public API ────────────────────────────────────────────────────────────────

def parse_for_split(spec: str, total_pages: int) -> list[list[int]]:
    """Parse a page spec and return groups of 0-indexed page numbers for splitting.

    Each group becomes a separate output PDF.

    Examples (all 0-indexed in output):
      parse_for_split("1,5,9", 10) → [[0,1,2,3], [4,5,6,7], [8,9]]
      parse_for_split("3-5", 10)   → [[2,3,4]]          (extract pages 3-5 only)
      parse_for_split("2s", 8)     → [[0,1],[2,3],[4,5],[6,7]]
      parse_for_split("1s", 4)     → [[0],[1],[2],[3]]
    """
    if total_pages < 1:
        raise ValueError("Document must have at least 1 page")

    mode = _detect_mode(spec)

    if mode == "stride":
        n = _parse_stride(spec, total_pages)
        groups = []
        for start in range(0, total_pages, n):
            groups.append(list(range(start, min(start + n, total_pages))))
        return groups

    if mode == "range":
        start, end = _parse_range(spec, total_pages)
        return [list(range(start, end + 1))]

    # list mode — interpret as split-points (1-indexed start of each chunk)
    points = _parse_list(spec, total_pages)
    # Deduplicate and sort; ensure 0 is always the first split point
    points = sorted(set([0] + points))  # 0-indexed; first point is always 0
    groups = []
    for i, start in enumerate(points):
        end = points[i + 1] if i + 1 < len(points) else total_pages
        groups.append(list(range(start, end)))
    return [g for g in groups if g]  # drop empty groups


def parse_for_delete(spec: str, total_pages: int) -> list[int]:
    """Parse a page spec and return a sorted list of 0-indexed pages to remove.

    Examples (all 0-indexed in output):
      parse_for_delete("1,5,9", 10) → [0, 4, 8]
      parse_for_delete("3-5", 10)   → [2, 3, 4]
      parse_for_delete("2s", 8)     → [1, 3, 5, 7]   (every 2nd page: pages 2,4,6,8)
    """
    if total_pages < 1:
        raise ValueError("Document must have at least 1 page")

    mode = _detect_mode(spec)

    if mode == "stride":
        n = _parse_stride(spec, total_pages)
        if n == 1:
            raise ValueError("'1s' would delete every page — nothing would remain")
        # Every Nth page (1-indexed): page N, 2N, 3N, ...
        return [i for i in range(n - 1, total_pages, n)]  # 0-indexed

    if mode == "range":
        start, end = _parse_range(spec, total_pages)
        if end - start + 1 >= total_pages:
            raise ValueError("Spec would delete all pages — nothing would remain")
        return list(range(start, end + 1))

    # list mode — explicit pages to delete
    pages = _parse_list(spec, total_pages)
    pages = sorted(set(pages))
    if len(pages) >= total_pages:
        raise ValueError("Spec would delete all pages — nothing would remain")
    return pages


# ── CLI self-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    tests = [
        ("split", "1,5,9", 10, [[0,1,2,3],[4,5,6,7],[8,9]]),
        ("split", "3-5",   10, [[2,3,4]]),
        ("split", "2s",    8,  [[0,1],[2,3],[4,5],[6,7]]),
        ("split", "1s",    4,  [[0],[1],[2],[3]]),
        ("delete","1,5,9", 10, [0,4,8]),
        ("delete","3-5",   10, [2,3,4]),
        ("delete","2s",    8,  [1,3,5,7]),
    ]

    ok = True
    for op, spec, total, expected in tests:
        fn = parse_for_split if op == "split" else parse_for_delete
        result = fn(spec, total)
        status = "OK" if result == expected else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"{status}  {op}({spec!r}, {total}) → {result}  (expected {expected})")

    sys.exit(0 if ok else 1)
