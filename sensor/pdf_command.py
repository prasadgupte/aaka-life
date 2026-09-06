#!/usr/bin/env python3
"""
sensor/pdf_command.py — channel-agnostic `/pdf` (and `p …`) command handler.

PDF operations can't run inside the router subprocess: they have to send *files*
back, and the router talks to its poller over stdout text only. Every poller
therefore intercepts `/pdf` before dispatching to the router — this module is
that interception, factored out so Telegram and Signal share one implementation
instead of two copies drifting apart.

A poller wires it up once with its own delivery callables:

    _PDF = PdfCommands(
        channel="signal",
        media_dir=CONFIG_DIR / "data" / "signal_media",
        send_reply=_send_reply,        # (chat_id, text, reply_to=None)
        send_document=_send_document,  # (chat_id, path, caption="", reply_to=None)
    )
    if is_pdf_command(text):
        _PDF.handle(chat_id, message_id, text, media_path, filename, sender_id)

Behaviour (identical for every channel):
  • `/pdf help|compress|extract|ocr|split|merge|delete` — see tools/pdf_tool.py
  • `save [#tag …]` stages the result into the vault via a queued `drop_file`
    instead of sending it back
  • a trailing `ocr` chains OCR onto whatever the primary command delivered
  • `/pdf merge` consumes the per-chat stage that `stage()` fills as files arrive
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parents[1])
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))

_PDF_STAGE_TTL = 600  # seconds (10 min) before staged files expire

PDF_MIME_TYPES = {
    "application/pdf", "image/jpeg", "image/png", "image/gif",
    "image/webp", "image/bmp", "image/tiff", "image/heic", "image/heif",
}
PDF_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
                  ".webp", ".heic", ".heif"}


def is_pdf_command(text: str) -> bool:
    """True for `/pdf …` and the single-letter alias `p …`."""
    t = (text or "").strip()
    return bool(re.match(r"^/pdf\b", t, re.IGNORECASE) or re.match(r"^p\s", t, re.IGNORECASE))


class PdfCommands:
    def __init__(self, *, channel: str, media_dir: Path,
                 send_reply, send_document, log: "logging.Logger | None" = None):
        self.channel = channel
        self.media_dir = Path(media_dir)
        self._send_reply = send_reply
        self._send_document = send_document
        self._log = log or logging.getLogger(f"pdf_command.{channel}")
        # chat_id → [{"path", "filename", "ts"}]
        self._stage: dict = {}

    # ── merge staging ─────────────────────────────────────────────────────────

    def stage(self, chat_id: str, media_path: str, filename: str, mime_type: str) -> None:
        """Record a downloaded file in the per-chat `/pdf merge` stage."""
        ext = Path(filename).suffix.lower()
        is_pdf = mime_type == "application/pdf" or ext == ".pdf"
        is_img = mime_type in PDF_MIME_TYPES or ext in PDF_IMAGE_EXTS
        if not (is_pdf or is_img):
            return
        now = time.time()
        self._stage[chat_id] = [
            e for e in self._stage.get(chat_id, []) if now - e["ts"] < _PDF_STAGE_TTL
        ]
        self._stage[chat_id].append({"path": media_path, "filename": filename, "ts": now})
        self._log.info("pdf_stage chat=%s staged %s (%d total)", chat_id, filename,
                       len(self._stage[chat_id]))

    # ── vault hand-off ────────────────────────────────────────────────────────

    def _drop_file(self, result_path: str, tags: list, sender_id: str,
                   chat_id: str, message_id) -> str:
        """Stage a PDF result and queue it as drop_file for vault routing.
        Returns a confirmation string like '📎 Saved → result.pdf #receipts'."""
        import shutil as _shutil
        import uuid as _uuid

        from aaka_queue.queue import update_status, write_item
        from tools.file_utils import sanitize_filename

        staging_id = _uuid.uuid4().hex[:12]
        staging_dir = CONFIG_DIR / "data" / "staging" / staging_id
        staging_dir.mkdir(parents=True, exist_ok=True)

        filename = Path(result_path).name
        safe_name = sanitize_filename(filename)
        dest = staging_dir / safe_name
        _shutil.copy2(result_path, str(dest))

        try:
            import aaka_config as _cfg
            member = _cfg.member_by_sender(sender_id)
            namespace = member["id"] if member else "user"
        except Exception:
            namespace = "user"

        payload = {
            "tags": tags,
            "media_staging_path": f"staging/{staging_id}/{safe_name}",
            "mime_type": "application/pdf",
            "original_filename": filename,
            "file_size_bytes": dest.stat().st_size,
            "caption": " ".join(f"#{t}" for t in tags),
            "namespace": namespace,
            "message_id": str(message_id),
            "source": self.channel,
        }
        item_id = write_item(
            intent="drop_file", raw_message="pdf save",
            sender=sender_id, channel_id=chat_id,
            source=self.channel, payload=payload,
        )
        update_status(item_id, "confirmed")

        tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
        return f"📎 Saved → {safe_name} {tag_str} `#{item_id[:8]}`"

    # ── the command itself ────────────────────────────────────────────────────

    def handle(self, chat_id: str, message_id, text: str,
               media_path: "str | None", media_filename: str = "",
               sender_id: "str | None" = None) -> bool:
        """Handle a `/pdf` command. Returns True when it was handled (the caller
        must then skip router dispatch), False when this isn't a PDF command."""
        sender_id = sender_id or chat_id

        t = text.strip()
        if re.match(r"^p\s", t, re.IGNORECASE):
            t = "/pdf " + t[2:].strip()
        if not re.match(r"^/pdf\b", t, re.IGNORECASE):
            return False

        parts = t.split(None, 2)  # ["/pdf", command?, args?]
        command = parts[1].lower() if len(parts) > 1 else "help"
        args = parts[2].strip() if len(parts) > 2 else ""

        # Parse save flag and optional tags: "save" or "save #receipts #work"
        _save_match = re.search(r'\bsave(?:\s+((?:#\w+\s*)+))?\b', args, re.I)
        save_mode = bool(_save_match)
        save_tags = []
        if _save_match and _save_match.group(1):
            save_tags = [x.lstrip("#") for x in _save_match.group(1).split()]
        args_clean = re.sub(r'\bsave(?:\s+(?:#\w+\s*)+)?\b', '', args, flags=re.I).strip()

        # Trailing `ocr` chain keyword: OCR every PDF the primary command delivers.
        ocr_chain = bool(re.search(r'\bocr\b', args_clean, re.I))
        if ocr_chain:
            args_clean = re.sub(r'\bocr\b', '', args_clean, flags=re.I).strip()

        self._log.info("pdf_cmd chat=%s cmd=%r args=%r save=%s tags=%s ocr=%s media=%s",
                       chat_id, command, args_clean, save_mode, save_tags, ocr_chain, media_path)

        def _deliver(file_path: str, caption: str, tags: list = None) -> None:
            if save_mode:
                tg = tags or save_tags or []
                msg = self._drop_file(file_path, tg, sender_id=sender_id,
                                      chat_id=chat_id, message_id=message_id)
                self._send_reply(chat_id, msg, reply_to=message_id)
            else:
                self._send_document(chat_id, file_path, caption=caption, reply_to=message_id)

            if ocr_chain and file_path.lower().endswith(".pdf"):
                try:
                    r = _pdf_ocr(file_path)
                    self._send_document(chat_id, r["text_file"],
                                        caption=f"OCR: {r['ocr_pages']}/{r['pages']} pages, "
                                                f"{r['chars']} chars",
                                        reply_to=message_id)
                except Exception as exc:
                    self._send_reply(chat_id, f"⚠️ OCR failed: {exc}", reply_to=message_id)

        try:
            from tools.pdf_tool import (
                compress, delete_blank_pages, delete_pages, extract, help_text, merge,
                ocr as _pdf_ocr, split,
            )

            _pdf_out = self.media_dir / "pdf_output"
            _pdf_out.mkdir(parents=True, exist_ok=True)

            def _require_media() -> str:
                if not media_path or not Path(media_path).exists():
                    raise ValueError("Please attach a PDF file to use this command.")
                return media_path

            if command == "help":
                self._send_reply(chat_id, help_text(), reply_to=message_id)

            elif command == "compress":
                src = _require_media()
                out = str(_pdf_out / f"{Path(src).stem}_compressed.pdf")
                q_match = re.search(r'\bq(?:uality)?=(\d+)\b', args_clean, re.I)
                d_match = re.search(r'\bdpi=(\d+)\b', args_clean, re.I)
                kw = {}
                if q_match:
                    kw["quality"] = max(1, min(95, int(q_match.group(1))))
                if d_match:
                    kw["dpi"] = int(d_match.group(1))
                r = compress(src, out, **kw)
                imgs = f", {r['images_processed']} images" if r['images_processed'] else ""
                _deliver(r["output"],
                         f"Compressed: {r['original_kb']} KB → {r['compressed_kb']} KB{imgs}")

            elif command == "extract":
                src = _require_media()
                r = extract(src, str(_pdf_out))
                _deliver(r["text_file"], f"Extracted {r['pages']} pages")

            elif command == "ocr":
                src = _require_media()
                out = str(_pdf_out / f"{Path(src).stem}_text.txt")
                lang_match = re.search(r'\blang(?:uage)?=([\w+]+)', args_clean, re.I)
                language = lang_match.group(1) if lang_match else "eng"
                r = _pdf_ocr(src, out, language=language)
                _deliver(r["text_file"],
                         f"OCR: {r['ocr_pages']}/{r['pages']} pages, {r['chars']} chars")

            elif command == "split":
                if not args_clean:
                    raise ValueError("Usage: /pdf split <spec>  e.g. /pdf split 2s or /pdf split 1,5,9")
                src = _require_media()
                r = split(src, args_clean, str(_pdf_out))
                if save_mode:
                    for path in r["outputs"]:
                        self._drop_file(path, save_tags, sender_id=sender_id,
                                        chat_id=chat_id, message_id=message_id)
                    self._send_reply(chat_id, f"📎 Saved {r['count']} parts to vault.",
                                     reply_to=message_id)
                else:
                    self._send_reply(chat_id, f"Split into {r['count']} files — sending…",
                                     reply_to=message_id)
                    for path in r["outputs"]:
                        self._send_document(chat_id, path)

            elif command == "merge":
                now = time.time()
                entries = [
                    e for e in self._stage.get(chat_id, [])
                    if now - e["ts"] < _PDF_STAGE_TTL and Path(e["path"]).exists()
                ]
                if not entries:
                    self._send_reply(chat_id,
                                     "No files staged for merge.\n"
                                     "Send your PDFs/images first, then /pdf merge.",
                                     reply_to=message_id)
                else:
                    paths = [e["path"] for e in entries]
                    out = str(_pdf_out / f"{Path(paths[0]).stem}_merged.pdf")
                    r = merge(paths, out)
                    _deliver(r["output"], f"Merged {len(paths)} files → {r['pages']} pages")
                    self._stage.pop(chat_id, None)

            elif command == "delete":
                if not args_clean:
                    raise ValueError("Usage: /pdf delete <spec>  e.g. /pdf delete 3-5 or /pdf delete 2s")
                first_tok = args_clean.split(None, 1)[0].lower()
                if first_tok in ("blank-pages", "blanks", "blank"):
                    rest = args_clean[len(first_tok):].lower()
                    dont_return = bool(re.search(r'\b(dont[-]?return|no[-]?blanks)\b', rest))
                    src = _require_media()
                    stem = Path(src).stem
                    out = str(_pdf_out / f"{stem}_trimmed.pdf")
                    blanks_out = str(_pdf_out / f"{stem}_blanks.pdf")
                    r = delete_blank_pages(src, out, blanks_out, write_blanks=not dont_return)
                    pages = r["blank_pages"]
                    pages_str = ",".join(map(str, pages)) if 0 < len(pages) <= 20 else ""
                    cap_main = (
                        f"Removed {r['removed']} blank pages, {r['remaining']} remaining"
                        + (f" (pages: {pages_str})" if pages_str else "")
                    )
                    _deliver(r["output"], cap_main)
                    if r["blanks_output"]:
                        _deliver(r["blanks_output"], "Removed pages — verify they were blank")
                else:
                    src = _require_media()
                    out = str(_pdf_out / f"{Path(src).stem}_trimmed.pdf")
                    r = delete_pages(src, args_clean, out)
                    _deliver(r["output"],
                             f"Removed {r['removed']} pages, {r['remaining']} remaining")

            else:
                self._send_reply(chat_id, f"Unknown PDF command: {command!r}\n\n" + help_text(),
                                 reply_to=message_id)

        except (ValueError, FileNotFoundError) as exc:
            self._send_reply(chat_id, f"⚠️ {exc}", reply_to=message_id)
        except ImportError:
            self._send_reply(chat_id, "⚠️ pymupdf not installed on this node.",
                             reply_to=message_id)
        except Exception as exc:
            self._log.error("pdf_cmd error: %s", exc, exc_info=True)
            self._send_reply(chat_id, f"⚠️ PDF error: {exc}", reply_to=message_id)

        return True
