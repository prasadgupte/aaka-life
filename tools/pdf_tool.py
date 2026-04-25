"""
tools/pdf_tool.py — PDF manipulation library and CLI.

Operations:
  compress    Reduce file size (stream compression + image recompression)
  extract     Extract metadata and per-page text; returns PDF copy + .txt sidecar
  split       Split into multiple PDFs by page spec
  merge       Merge PDFs and images into a single PDF in order
  delete      Remove pages by page spec

All functions are pure (file paths in, file paths out). No Telegram or HTTP awareness.

Requires: pymupdf>=1.25  (pip install pymupdf)

CLI usage:
  python3 tools/pdf_tool.py compress input.pdf [-o out.pdf] [-q 80]
  python3 tools/pdf_tool.py extract  input.pdf [-o ./output_dir/]
  python3 tools/pdf_tool.py split    input.pdf "1,5,9" [-o ./output_dir/]
  python3 tools/pdf_tool.py merge    a.pdf b.pdf photo.jpg [-o combined.pdf]
  python3 tools/pdf_tool.py delete   input.pdf "3-5" [-o trimmed.pdf]
"""

import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path when run directly
_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from tools.pdf_page_spec import parse_for_split, parse_for_delete

# Image extensions that can be merged into a PDF
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif"}

# Blank-page detection thresholds. Tuned to err on the side of *keeping* pages —
# the blanks companion PDF lets the user verify, but a false-positive deletion of
# a real page (e.g. one that contains only a page number) is harder to notice.
_BLANK_INK_RATIO = 0.003         # < 0.3% non-white pixels (full page) → blank
_BLANK_WHITE_BYTE = 240          # grayscale byte ≥ this counts as "white"
_BLANK_DETECT_DPI = 72           # raster dpi for the pixel-ratio test (cheap)
# Interior-only fallback for scanner-shadow blanks: a duplex-scanned empty page
# often has a faint dark band hugging one edge from the scanner gap. The full
# page has 0.5–2% non-white pixels (above the main threshold), but the interior
# is essentially pure white. Measured on real shadow blanks: ~0.001% interior
# ink vs ~2.2% on the sparsest real content page — ~2000× separation.
_BLANK_INTERIOR_MARGIN = 0.08    # drop outer 8% of width+height before checking
_BLANK_INTERIOR_INK_RATIO = 0.0005  # < 0.05% non-white in interior → blank


def _open_pdf(path: str):
    """Open a PDF with fitz; raises ValueError for bad paths or non-PDFs."""
    import fitz
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    doc = fitz.open(str(p))
    if doc.is_encrypted:
        raise ValueError(f"PDF is encrypted/password-protected: {path}")
    return doc


def _kb(path: str) -> int:
    return max(1, os.path.getsize(path) // 1024)


def _default_output(input_path: str, suffix: str, output_path: str = "") -> str:
    """Return output_path if set, else derive from input with suffix."""
    if output_path:
        return output_path
    p = Path(input_path)
    return str(p.with_name(p.stem + suffix + p.suffix))


def _default_dir(output_dir: str, input_path: str) -> Path:
    """Return output_dir as Path (defaults to same directory as input)."""
    if output_dir:
        d = Path(output_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(input_path).parent


# ── Operations ────────────────────────────────────────────────────────────────

def compress(input_path: str, output_path: str = "", *,
             quality: int = 80, dpi: int = 0) -> dict:
    """Compress a PDF.

    Args:
        quality: JPEG re-encoding quality 1–95 (lower = smaller file, default 80).
                 Applied to all colour images. 60 is visually acceptable for most docs.
        dpi:     If > 0, downsample images whose effective resolution exceeds this value.
                 E.g. dpi=150 shrinks a 300-DPI scan to half its pixel dimensions (4×
                 fewer pixels). 150 is fine for screen/email; 200 for readable print.
                 0 means no downsampling (default).

    Returns:
        {"output": str, "original_kb": int, "compressed_kb": int, "images_processed": int}
    """
    import fitz

    output_path = _default_output(input_path, "_compressed", output_path)
    doc = _open_pdf(input_path)

    # Max pixel dimension when dpi is requested.
    # Assumes images are placed across a full US-letter/A4 width (~8.5 in).
    # At dpi=150 → cap at 1275 px wide; at dpi=200 → 1700 px.
    max_px = int(dpi * 8.5) if dpi > 0 else 0

    processed = 0
    seen_xrefs: set = set()

    for page in doc:
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                # Skip masks and soft-mask images
                if img_info[1] != 0:  # smask xref != 0 means this IS a smask
                    pass  # still process the base image
                colorspace = doc.xref_get_key(xref, "ColorSpace")
                # Skip masks (1-bit images) — JBIG2/CCITTFax scanned text
                filters = str(doc.xref_get_key(xref, "Filter"))
                if "JBIG2" in filters or "CCITTFax" in filters:
                    continue

                # Decode via xref — works for JPEG, JPEG2000, uncompressed, etc.
                pix = fitz.Pixmap(doc, xref)

                # Skip images with alpha channel or tiny thumbnails
                if pix.alpha or pix.width * pix.height < 4096:
                    pix = None
                    continue

                # Convert CMYK → RGB (JPEG encoder needs RGB or greyscale)
                if pix.colorspace and pix.colorspace.n == 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                # Downsample if dpi requested and image is oversized
                if max_px > 0 and max(pix.width, pix.height) > max_px:
                    scale = max_px / max(pix.width, pix.height)
                    new_w = max(1, int(pix.width * scale))
                    new_h = max(1, int(pix.height * scale))
                    pix = fitz.Pixmap(pix, new_w, new_h)

                cs_name = "/DeviceGray" if (pix.colorspace and pix.colorspace.n == 1) else "/DeviceRGB"
                jpeg_bytes = pix.tobytes("jpeg", jpg_quality=quality)
                pix = None

                # Compare against the encoded (on-disk) stream, not the decoded pixels.
                # Use compress=False so the JPEG lands as DCTDecode, not FlateDecode(JPEG).
                raw_stream = doc.xref_stream_raw(xref)
                if raw_stream and len(jpeg_bytes) < len(raw_stream):
                    doc.update_stream(xref, jpeg_bytes, compress=False)
                    doc.xref_set_key(xref, "Filter", "/DCTDecode")
                    doc.xref_set_key(xref, "ColorSpace", cs_name)
                    doc.xref_set_key(xref, "BitsPerComponent", "8")
                    processed += 1

            except Exception:
                continue  # skip any xref we can't safely process

    # ── Text-PDF optimisations ────────────────────────────────────────────────
    # Font subsetting: strip unused glyph data from embedded fonts (TrueType/OTF).
    # MuPDF-native (fitz >= 1.23); non-fatal if it fails on exotic CJK fonts.
    try:
        doc.subset_fonts()
    except Exception:
        pass

    # Per-page thumbnails: Word/Acrobat often embed these; removing saves 10–50 KB/page.
    thumbs_stripped = 0
    try:
        for pno in range(len(doc)):
            px = doc.page_xref(pno)
            if doc.xref_get_key(px, "Thumb") != "null":
                doc.xref_set_key(px, "Thumb", "null")
                thumbs_stripped += 1
    except Exception:
        pass

    # XMP + /Info metadata: safe to strip unless the file is PDF/A (which requires XMP).
    try:
        xmp = doc.get_xml_metadata() or ""
        is_pdfa = "pdfaid:conformance" in xmp or "pdfaid:part" in xmp
        if not is_pdfa:
            doc.del_xml_metadata()
            doc.set_metadata({})
    except Exception:
        pass

    doc.save(
        output_path,
        garbage=4,           # remove unused objects + deduplicate streams
        deflate=True,        # compress all non-image streams
        deflate_fonts=True,  # compress embedded font program data
        clean=True,          # sanitize content streams
        linear=False,
    )
    doc.close()

    # Scan fallback: CCITT/JBIG2 images (typical iOS scans) are skipped above.
    # If nothing was recompressed and the file is large, rasterize each page as JPEG.
    if processed == 0 and _kb(input_path) >= 1024:
        import os as _os
        raster_tmp = output_path + ".raster_tmp.pdf"
        try:
            doc2 = _open_pdf(input_path)
            new_pdf = fitz.open()
            raster_dpi = max(dpi, 150) if dpi > 0 else 150
            raster_quality = min(quality, 70)
            page_count = 0
            for pg in doc2:
                pix = pg.get_pixmap(dpi=raster_dpi, alpha=False)
                jpeg_bytes = pix.tobytes("jpeg", jpg_quality=raster_quality)
                pix = None
                new_page = new_pdf.new_page(width=pg.rect.width, height=pg.rect.height)
                new_page.insert_image(pg.rect, stream=jpeg_bytes)
                page_count += 1
            new_pdf.save(raster_tmp, garbage=4, deflate=True, clean=True)
            new_pdf.close()
            doc2.close()
            if _kb(raster_tmp) < _kb(output_path):
                _os.replace(raster_tmp, output_path)
                processed = page_count
            else:
                _os.unlink(raster_tmp)
        except Exception:
            _rt = Path(raster_tmp)
            if _rt.exists():
                _rt.unlink()

    return {
        "output": output_path,
        "original_kb": _kb(input_path),
        "compressed_kb": _kb(output_path),
        "images_processed": processed,
    }


def extract(input_path: str, output_dir: str = "") -> dict:
    """Extract metadata and per-page text from a PDF.

    Writes a .txt sidecar next to (or into output_dir) the copied PDF.

    Returns:
        {"output_pdf": str, "text_file": str, "pages": int, "metadata": dict}
    """
    import shutil

    doc = _open_pdf(input_path)
    out_dir = _default_dir(output_dir, input_path)

    stem = Path(input_path).stem
    out_pdf = str(out_dir / (stem + "_extracted.pdf"))
    out_txt = str(out_dir / (stem + "_text.txt"))

    # Copy PDF unchanged
    shutil.copy2(input_path, out_pdf)

    # Extract metadata
    meta = {k: v for k, v in (doc.metadata or {}).items() if v}

    # Extract per-page text
    lines = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text").strip()
        lines.append(f"--- Page {i} ---")
        lines.append(text if text else "(no text)")
        lines.append("")

    page_count = len(doc)
    doc.close()

    Path(out_txt).write_text("\n".join(lines), encoding="utf-8")

    return {
        "output_pdf": out_pdf,
        "text_file": out_txt,
        "pages": page_count,
        "metadata": meta,
    }


def split(input_path: str, spec: str, output_dir: str = "") -> dict:
    """Split a PDF into multiple files by page spec.

    Returns:
        {"outputs": [str, ...], "count": int}
    """
    import fitz

    doc = _open_pdf(input_path)
    total = len(doc)
    out_dir = _default_dir(output_dir, input_path)
    stem = Path(input_path).stem

    groups = parse_for_split(spec, total)
    outputs = []

    for idx, pages in enumerate(groups, start=1):
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=pages[0], to_page=pages[-1])
        out_path = str(out_dir / f"{stem}_part{idx:02d}.pdf")
        new_doc.save(out_path, garbage=3, deflate=True)
        new_doc.close()
        outputs.append(out_path)

    doc.close()
    return {"outputs": outputs, "count": len(outputs)}


def merge(input_paths: list, output_path: str = "") -> dict:
    """Merge multiple PDFs and images into a single PDF in the given order.

    Images are converted to single-page PDFs before merging.

    Returns:
        {"output": str, "pages": int}
    """
    import fitz

    if not input_paths:
        raise ValueError("No input files provided for merge")

    if not output_path:
        # Derive from first input
        p = Path(input_paths[0])
        output_path = str(p.with_name(p.stem + "_merged.pdf"))

    merged = fitz.open()

    for path in input_paths:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")

        ext = p.suffix.lower()

        if ext in _IMAGE_EXTS:
            # Convert image to a single-page PDF
            img_doc = fitz.open()
            img = fitz.open(str(p))  # fitz can open images directly
            rect = img[0].rect
            page = img_doc.new_page(width=rect.width, height=rect.height)
            page.insert_image(rect, filename=str(p))
            img.close()
            merged.insert_pdf(img_doc)
            img_doc.close()
        else:
            src = _open_pdf(str(p))
            merged.insert_pdf(src)
            src.close()

    total_pages = len(merged)
    merged.save(output_path, garbage=3, deflate=True)
    merged.close()

    return {"output": output_path, "pages": total_pages}


def delete_pages(input_path: str, spec: str, output_path: str = "") -> dict:
    """Remove pages from a PDF by page spec.

    Returns:
        {"output": str, "removed": int, "remaining": int}
    """
    import fitz

    output_path = _default_output(input_path, "_trimmed", output_path)
    doc = _open_pdf(input_path)
    total = len(doc)

    to_remove = parse_for_delete(spec, total)
    keep = [i for i in range(total) if i not in set(to_remove)]

    new_doc = fitz.open()
    # Insert pages we want to keep (select by iterating keep list as ranges)
    # Group consecutive keep pages for efficiency
    if keep:
        i = 0
        while i < len(keep):
            start = keep[i]
            end = start
            while i + 1 < len(keep) and keep[i + 1] == keep[i] + 1:
                i += 1
                end = keep[i]
            new_doc.insert_pdf(doc, from_page=start, to_page=end)
            i += 1

    doc.close()
    new_doc.save(output_path, garbage=3, deflate=True)
    new_doc.close()

    return {
        "output": output_path,
        "removed": len(to_remove),
        "remaining": len(keep),
    }


def _is_blank_page(page) -> bool:
    """Return True if a fitz page has no meaningful content.

    Text and drawings checks short-circuit first. After that, a page is blank
    if EITHER:
      - the full-page non-white pixel ratio is below _BLANK_INK_RATIO, OR
      - the interior (after cropping a margin) is essentially pure white —
        catches scanner-shadow blanks from duplex scanning where a dark band
        along one edge inflates the full-page ratio above the main threshold.
    """
    import fitz

    text = page.get_text("text") or ""
    if "".join(text.split()):  # any non-whitespace text → not blank
        return False

    if page.get_drawings():
        return False

    pix = page.get_pixmap(dpi=_BLANK_DETECT_DPI, colorspace=fitz.csGRAY, alpha=False)
    samples = pix.samples
    total = len(samples)
    if total == 0:
        return True

    non_white = sum(1 for b in samples if b < _BLANK_WHITE_BYTE)
    if (non_white / total) < _BLANK_INK_RATIO:
        return True

    # Interior-only fallback: ignore the outer margin band.
    w, h = pix.width, pix.height
    mx = int(w * _BLANK_INTERIOR_MARGIN)
    my = int(h * _BLANK_INTERIOR_MARGIN)
    iw, ih = w - 2 * mx, h - 2 * my
    if iw <= 0 or ih <= 0:
        return False
    interior_non_white = 0
    for y in range(my, h - my):
        row = samples[y * w + mx : y * w + (w - mx)]
        interior_non_white += sum(1 for b in row if b < _BLANK_WHITE_BYTE)
    return (interior_non_white / (iw * ih)) < _BLANK_INTERIOR_INK_RATIO


def _insert_grouped(dst, src, page_indices: list) -> None:
    """Insert pages from src into dst, grouping consecutive indices for efficiency."""
    if not page_indices:
        return
    i = 0
    while i < len(page_indices):
        start = page_indices[i]
        end = start
        while i + 1 < len(page_indices) and page_indices[i + 1] == page_indices[i] + 1:
            i += 1
            end = page_indices[i]
        dst.insert_pdf(src, from_page=start, to_page=end)
        i += 1


def _tessdata_dir() -> "str | None":
    """Locate the tessdata directory on this host. Returns None if TESSDATA_PREFIX
    is already set (let fitz honour the env) or no known location exists."""
    if os.environ.get("TESSDATA_PREFIX"):
        return None
    for candidate in (
        "/opt/homebrew/share/tessdata",       # Mac (Apple Silicon brew)
        "/usr/local/share/tessdata",          # Mac (Intel brew)
        "/usr/share/tesseract-ocr/5/tessdata",
        "/usr/share/tesseract-ocr/4.00/tessdata",
        "/usr/share/tessdata",                # Debian/Ubuntu
    ):
        if os.path.isdir(candidate):
            return candidate
    return None


def ocr(input_path: str, output_path: str = "", *, language: str = "eng") -> dict:
    """OCR a PDF page-by-page using Tesseract via PyMuPDF.

    Writes a plain-text .txt file with `--- Page N ---` separators. Pages that
    already contain a text layer fall back to it (no re-OCR needed).

    Args:
        language: Tesseract language code(s), e.g. 'eng' or 'eng+deu'.

    Returns:
        {"text_file": str, "pages": int, "chars": int, "ocr_pages": int}
        ocr_pages = pages where OCR was actually invoked (no native text layer).

    Requires: tesseract binary on PATH (and matching language data).
    """
    doc = _open_pdf(input_path)
    if not output_path:
        p = Path(input_path)
        output_path = str(p.with_name(p.stem + "_text.txt"))

    tessdata = _tessdata_dir()
    sections: list = []
    ocr_pages = 0

    for i, page in enumerate(doc, start=1):
        native = (page.get_text("text") or "").strip()
        if native and len(native) >= 20:
            text = native
        else:
            kwargs = {"language": language, "full": True}
            if tessdata:
                kwargs["tessdata"] = tessdata
            try:
                tp = page.get_textpage_ocr(**kwargs)
                text = (tp.extractText() or "").strip()
                ocr_pages += 1
            except Exception as e:
                text = f"(OCR failed: {e})"
        sections.append(f"--- Page {i} ---\n{text or '(empty)'}\n")

    body = "\n".join(sections)
    Path(output_path).write_text(body, encoding="utf-8")
    pages = len(doc)
    doc.close()

    return {
        "text_file": output_path,
        "pages": pages,
        "chars": len(body),
        "ocr_pages": ocr_pages,
    }


def delete_blank_pages(input_path: str, output_path: str = "",
                       blanks_path: str = "", *,
                       write_blanks: bool = True) -> dict:
    """Auto-detect and remove blank pages.

    Args:
        write_blanks: if False, do not write the *_blanks.pdf companion file.

    Returns:
        {"output": str, "blanks_output": str | None,
         "removed": int, "remaining": int, "blank_pages": list[int]}
        Page numbers in blank_pages are 1-based.
    """
    import fitz

    output_path = _default_output(input_path, "_trimmed", output_path)
    doc = _open_pdf(input_path)
    total = len(doc)

    blank_idx = [i for i in range(total) if _is_blank_page(doc[i])]
    keep_idx = [i for i in range(total) if i not in set(blank_idx)]

    if not keep_idx:
        doc.close()
        raise ValueError(
            f"All {total} pages detected as blank — refusing to write an empty PDF. "
            "If this looks wrong, the detector may be too aggressive for this scan."
        )

    trimmed = fitz.open()
    _insert_grouped(trimmed, doc, keep_idx)
    trimmed.save(output_path, garbage=3, deflate=True)
    trimmed.close()

    blanks_output = None
    if write_blanks and blank_idx:
        if blanks_path:
            blanks_output = blanks_path
        else:
            p = Path(input_path)
            blanks_output = str(p.with_name(p.stem + "_blanks.pdf"))
        blanks_doc = fitz.open()
        _insert_grouped(blanks_doc, doc, blank_idx)
        blanks_doc.save(blanks_output, garbage=3, deflate=True)
        blanks_doc.close()

    doc.close()

    return {
        "output": output_path,
        "blanks_output": blanks_output,
        "removed": len(blank_idx),
        "remaining": len(keep_idx),
        "blank_pages": [i + 1 for i in blank_idx],
    }


# ── Help text ─────────────────────────────────────────────────────────────────

_HELP = """\
📄 *PDF Tool*

*Compress*
  p compress                  Quality=80, no downsampling
  p compress q=60             Lower JPEG quality (1–95). 60 = good for most docs, 40 = aggressive
  p compress dpi=150          Downsample images to 150 DPI — best for scanned docs (300→150 = ~4× smaller)
  p compress dpi=200          Conservative downsample — text stays sharp for printing
  p compress q=60 dpi=150     Both — maximum reduction for scans/photos
  p compress … save           Save result to vault (via /drop) instead of sending back
  p compress … save #receipts  Save with tag

*Split / Delete / Extract*
  p split 1,5,9               Split at pages 1, 5, 9 → 3 PDFs
  p split 3-5                 Extract pages 3–5 only
  p split 2s                  Every 2 pages → separate PDF
  p delete 3-5                Remove pages 3–5
  p delete 1,3,5              Remove specific pages
  p delete 2s                 Remove every 2nd page
  p delete blank-pages        Auto-detect & remove blank pages; returns *_blanks.pdf to verify
  p delete blank-pages dont-return   Same, but don't send back the blanks file
  p extract                   Extract text + metadata → .txt file
  p ocr                       Run OCR (Tesseract) → .txt file (use for scanned PDFs)

*Chaining*
  Append `ocr` to any command to OCR its output last:
    p delete blank-pages ocr  Remove blanks, then OCR the trimmed PDF
  (any command) save          Save to vault instead of sending back
  (any command) save #tag     Save with tag

*Merge*
  p merge                     Merge all staged files in order
  p merge save                Merge and save to vault
  (send files first, then p merge)

*Page spec:*  `1,5,9` split-points · `3-5` range · `2s` every-N stride

Shortcut: `p` = `/pdf`  (e.g. `p compress q=60 dpi=150 save #work`)
"""


def help_text() -> str:
    return _HELP


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli():
    import argparse

    parser = argparse.ArgumentParser(
        prog="pdf_tool",
        description="PDF manipulation tool (compress / extract / split / merge / delete)",
    )
    sub = parser.add_subparsers(dest="command")

    # help
    sub.add_parser("help", help="Show command reference")

    # compress
    p = sub.add_parser("compress", help="Compress a PDF")
    p.add_argument("input", help="Input PDF path")
    p.add_argument("-o", "--output", default="", help="Output path")
    p.add_argument("-q", "--quality", type=int, default=80,
                   help="JPEG recompression quality 1-95 (default: 80, lower=smaller)")
    p.add_argument("--dpi", type=int, default=0,
                   help="Downsample images above this DPI (e.g. 150 for screen, 200 for print, 0=off)")

    # extract
    p = sub.add_parser("extract", help="Extract text and metadata")
    p.add_argument("input", help="Input PDF path")
    p.add_argument("-o", "--output-dir", default="", dest="output_dir",
                   help="Output directory (default: same as input)")

    # split
    p = sub.add_parser("split", help="Split into multiple PDFs")
    p.add_argument("input", help="Input PDF path")
    p.add_argument("spec", help="Page spec: '1,5,9', '3-5', or '2s'")
    p.add_argument("-o", "--output-dir", default="", dest="output_dir",
                   help="Output directory (default: same as input)")

    # merge
    p = sub.add_parser("merge", help="Merge PDFs and images into one PDF")
    p.add_argument("inputs", nargs="+", help="Input PDF/image paths (in merge order)")
    p.add_argument("-o", "--output", default="", help="Output path")

    # delete
    p = sub.add_parser("delete", help="Remove pages from a PDF")
    p.add_argument("input", help="Input PDF path")
    p.add_argument("spec", help="Pages to remove: '3-5', '1,3,5', or '2s'")
    p.add_argument("-o", "--output", default="", help="Output path")

    # ocr
    p = sub.add_parser("ocr",
                       help="OCR a PDF with Tesseract; writes <stem>_text.txt")
    p.add_argument("input", help="Input PDF path")
    p.add_argument("-o", "--output", default="", help="Output .txt path")
    p.add_argument("--language", default="eng",
                   help="Tesseract language code (default: eng). Combine with '+', e.g. 'eng+deu'.")

    # delete-blank-pages
    p = sub.add_parser("delete-blank-pages",
                       help="Auto-detect and remove blank pages (handles duplex scans)")
    p.add_argument("input", help="Input PDF path")
    p.add_argument("-o", "--output", default="", help="Trimmed output path")
    p.add_argument("--blanks-out", default="", dest="blanks_out",
                   help="Path for the removed-pages companion PDF")
    p.add_argument("--dont-return", action="store_true", dest="dont_return",
                   help="Do not write the *_blanks.pdf companion file")

    args = parser.parse_args()

    if not args.command or args.command == "help":
        print(help_text())
        return

    try:
        if args.command == "compress":
            r = compress(args.input, args.output, quality=args.quality, dpi=args.dpi)
            print(f"Compressed: {r['original_kb']} KB → {r['compressed_kb']} KB  ({r['images_processed']} images recompressed)")
            print(f"Output: {r['output']}")

        elif args.command == "extract":
            r = extract(args.input, args.output_dir)
            print(f"Extracted {r['pages']} pages")
            print(f"PDF:  {r['output_pdf']}")
            print(f"Text: {r['text_file']}")
            if r["metadata"]:
                for k, v in r["metadata"].items():
                    print(f"  {k}: {v}")

        elif args.command == "split":
            r = split(args.input, args.spec, args.output_dir)
            print(f"Split into {r['count']} files:")
            for p in r["outputs"]:
                print(f"  {p}")

        elif args.command == "merge":
            r = merge(args.inputs, args.output)
            print(f"Merged {len(args.inputs)} files → {r['pages']} pages")
            print(f"Output: {r['output']}")

        elif args.command == "delete":
            r = delete_pages(args.input, args.spec, args.output)
            print(f"Removed {r['removed']} pages, {r['remaining']} remaining")
            print(f"Output: {r['output']}")

        elif args.command == "ocr":
            r = ocr(args.input, args.output, language=args.language)
            print(f"OCR'd {r['ocr_pages']}/{r['pages']} pages "
                  f"({r['chars']} chars) → {r['text_file']}")

        elif args.command == "delete-blank-pages":
            r = delete_blank_pages(
                args.input, args.output, args.blanks_out,
                write_blanks=not args.dont_return,
            )
            pages = ", ".join(str(p) for p in r["blank_pages"]) or "(none)"
            print(f"Removed {r['removed']} blank pages: {pages}")
            print(f"Output: {r['output']}  ({r['remaining']} pages remaining)")
            if r["blanks_output"]:
                print(f"Blanks: {r['blanks_output']}")

    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ImportError:
        print("Error: pymupdf not installed. Run: pip install pymupdf", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
