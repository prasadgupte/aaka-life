#!/usr/bin/env python3
"""
sensor/telegram_poller.py — Direct Telegram long-poll loop.

Bypasses openclaw's Telegram connector (unreliable; silently stops polling).
Polls getUpdates, builds Format A metadata for router_sensor.py,
sends the reply back to Telegram directly.

Runs as a long-lived background process started by entrypoint.sh.
Auto-restart is handled by the caller (restart loop in entrypoint.sh).
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s poller %(levelname)s %(message)s",
)
_log = logging.getLogger("poller")

BASE = Path(os.environ.get("AAKA_BASE", "/app"))
CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# Offset persistence — our own file takes precedence; bootstrap from openclaw's on first run.
_OUR_OFFSET_FILE = CONFIG_DIR / "data" / "telegram_offset.json"
_OPENCLAW_OFFSET_FILE = Path("/home/aaka/.openclaw/telegram/update-offset-default.json")

POLL_TIMEOUT = 30  # seconds for Telegram long-poll

# ── PDF merge staging ──────────────────────────────────────────────────────────
# Maps chat_id → list of {"path": str, "filename": str, "ts": float}
# Populated whenever a PDF/image is downloaded. `/pdf merge` consumes and clears it.
_pdf_merge_stage: dict = {}
_PDF_STAGE_TTL = 600  # seconds (10 min) before staged files expire

_PDF_MIME_TYPES = {
    "application/pdf", "image/jpeg", "image/png", "image/gif",
    "image/webp", "image/bmp", "image/tiff", "image/heic", "image/heif",
}
_PDF_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif"}


# ── Telegram API helpers ────────────────────────────────────────────────────────

def _tg(method: str, params: dict = None, timeout: int = 35) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:200]
        if e.code in (502, 503, 504):
            # Telegram server errors — common during nightly maintenance; not actionable
            _log.warning("TG HTTP %s %s: %s", method, e.code, body)
        elif e.code == 429:
            _log.warning("TG HTTP %s 429: %s", method, body)
            try:
                retry_after = json.loads(body).get("parameters", {}).get("retry_after", 5)
            except Exception:
                retry_after = 5
            _log.info("TG rate-limited — sleeping %ss", retry_after)
            time.sleep(retry_after)
        else:
            _log.error("TG HTTP %s %s: %s", method, e.code, body)
        return {}
    except Exception as e:
        err_str = str(e)
        if method == "getUpdates" and any(k in err_str for k in ("timed out", "The read operation")):
            # Normal long-poll expiry — Telegram closes the connection after POLL_TIMEOUT seconds
            _log.debug("TG %s long-poll timeout (normal): %s", method, e)
        elif any(k in err_str for k in ("Connection reset", "Name resolution", "Temporary failure")):
            # Transient network blip — warn but don't alarm
            _log.warning("TG %s transient network error: %s", method, e)
        else:
            _log.error("TG %s error: %s", method, e)
        return {}



def _send_reply(chat_id: str, text: str, reply_to: int = None) -> None:
    if not text or not text.strip():
        return
    from gateway.egress import send, OutboundMessage, MessageKind
    try:
        send(OutboundMessage(
            kind=MessageKind.TEXT,
            recipient=str(chat_id),
            channel="telegram",
            text=text,
            source="telegram_poller",
            reply_to_message_id=str(reply_to) if reply_to else None,
        ))
    except Exception as e:
        _log.error("_send_reply egress error: %s", e)


def _send_document(chat_id: str, file_path: str, caption: str = "",
                   reply_to: int = None) -> None:
    """Send a file to Telegram via egress gateway."""
    from gateway.egress import send, OutboundMessage, MessageKind
    try:
        send(OutboundMessage(
            kind=MessageKind.DOCUMENT,
            recipient=str(chat_id),
            channel="telegram",
            file_path=file_path,
            file_caption=caption,
            source="telegram_poller",
            reply_to_message_id=str(reply_to) if reply_to else None,
        ))
    except Exception as e:
        _log.error("_send_document egress error: %s", e)


# ── Offset persistence ──────────────────────────────────────────────────────────

def _load_offset() -> int:
    """Return the next update_id to request.

    Priority: our own offset file → openclaw's offset file → 0.
    """
    for path in [_OUR_OFFSET_FILE, _OPENCLAW_OFFSET_FILE]:
        try:
            if path.exists():
                d = json.loads(path.read_text())
                uid = int(d.get("lastUpdateId", 0))
                if uid > 0:
                    _log.info("loaded offset %d from %s", uid, path.name)
                    return uid + 1
        except Exception:
            pass
    return 0


def _save_offset(last_update_id: int) -> None:
    _OUR_OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OUR_OFFSET_FILE.write_text(json.dumps({"lastUpdateId": last_update_id}))


# ── Format A metadata builder ──────────────────────────────────────────────────

def _build_format_a(sender_id: str, chat_id: str, message_id: int, text: str,
                     media_path: str = None, mime_type: str = None) -> str:
    """Wrap message in openclaw's Format A envelope.

    router_sensor.py's _parse_metadata() reads this to get sender_id,
    channel_id, message_id, and source ('telegram').
    The conversation_label 'id:<chat_id>' is parsed by the channel_id extractor.

    If media_path/mime_type are provided, prepends a [media attached: ...]
    header that _parse_media() in router_sensor.py can detect.
    """
    meta = {
        "chat_id": f"telegram:{chat_id}",
        "message_id": str(message_id),
        "sender_id": str(sender_id),
        "conversation_label": f"id:{chat_id}",
    }
    parts = [
        "Conversation info (untrusted metadata):\n"
        "```json\n"
        f"{json.dumps(meta)}\n"
        "```",
    ]
    if media_path and mime_type:
        parts.append(f"[media attached: {media_path} ({mime_type})]")
    parts.append(text)
    return "\n".join(parts)


# ── Telegram file download ─────────────────────────────────────────────────────

_MEDIA_DIR = CONFIG_DIR / "data" / "telegram_media"


def _download_tg_file(file_id: str, filename: str) -> "str | None":
    """Download a Telegram file by file_id. Returns local path or None."""
    resp = _tg("getFile", {"file_id": file_id}, timeout=15)
    file_path = (resp.get("result") or {}).get("file_path")
    if not file_path:
        _log.warning("getFile failed for file_id=%s", file_id)
        return None
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    # Use a temp file in the same dir then rename for atomicity
    fd, tmp_path = tempfile.mkstemp(dir=str(_MEDIA_DIR))
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        dest = _MEDIA_DIR / filename
        os.rename(tmp_path, str(dest))
        _log.info("downloaded %s (%d bytes)", dest.name, dest.stat().st_size)
        return str(dest)
    except Exception as e:
        _log.error("download failed for %s: %s", filename, e)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None


def _extract_media(msg: dict) -> "tuple[str, str, str] | None":
    """Extract media from a Telegram message.

    Returns (local_path, mime_type, original_filename) or None.
    Handles document, photo, video, audio, voice, video_note, sticker.
    """
    # Document (PDF, ZIP, etc.) — most relevant for /drop
    doc = msg.get("document")
    if doc:
        file_id = doc["file_id"]
        filename = doc.get("file_name", f"document_{file_id}")
        mime = doc.get("mime_type", "application/octet-stream")
        path = _download_tg_file(file_id, filename)
        return (path, mime, filename) if path else None

    # Photo — Telegram sends multiple sizes; pick the largest
    photos = msg.get("photo")
    if photos:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = best["file_id"]
        filename = f"photo_{file_id}.jpg"
        path = _download_tg_file(file_id, filename)
        return (path, "image/jpeg", filename) if path else None

    # Video
    video = msg.get("video")
    if video:
        file_id = video["file_id"]
        filename = video.get("file_name", f"video_{file_id}.mp4")
        mime = video.get("mime_type", "video/mp4")
        path = _download_tg_file(file_id, filename)
        return (path, mime, filename) if path else None

    # Audio
    audio = msg.get("audio")
    if audio:
        file_id = audio["file_id"]
        filename = audio.get("file_name", f"audio_{file_id}.mp3")
        mime = audio.get("mime_type", "audio/mpeg")
        path = _download_tg_file(file_id, filename)
        return (path, mime, filename) if path else None

    # Voice note
    voice = msg.get("voice")
    if voice:
        file_id = voice["file_id"]
        filename = f"voice_{file_id}.ogg"
        mime = voice.get("mime_type", "audio/ogg")
        path = _download_tg_file(file_id, filename)
        return (path, mime, filename) if path else None

    return None


# ── PDF command handler ────────────────────────────────────────────────────────

def _stage_for_pdf_merge(chat_id: str, media_path: str, filename: str,
                         mime_type: str) -> None:
    """Record a downloaded file in the per-chat PDF merge stage."""
    ext = Path(filename).suffix.lower()
    is_pdf = mime_type == "application/pdf" or ext == ".pdf"
    is_img = mime_type in _PDF_MIME_TYPES or ext in _PDF_IMAGE_EXTS
    if not (is_pdf or is_img):
        return
    now = time.time()
    # Expire old entries first
    _pdf_merge_stage[chat_id] = [
        e for e in _pdf_merge_stage.get(chat_id, [])
        if now - e["ts"] < _PDF_STAGE_TTL
    ]
    _pdf_merge_stage[chat_id].append({"path": media_path, "filename": filename, "ts": now})
    _log.info("pdf_stage chat=%s staged %s (%d total)", chat_id, filename,
              len(_pdf_merge_stage[chat_id]))


def _pdf_drop_file(result_path: str, tags: list, sender_id: str,
                   chat_id: str, message_id: int) -> str:
    """Stage a PDF result file and queue it as drop_file for vault routing.

    Returns a confirmation string like '📎 Saved → result.pdf #receipts'.
    """
    import uuid as _uuid
    import json as _json
    import shutil as _shutil
    sys.path.insert(0, str(BASE))
    from aaka_queue.queue import write_item, update_status
    from tools.file_utils import sanitize_filename

    config_dir = CONFIG_DIR
    staging_id = _uuid.uuid4().hex[:12]
    staging_dir = config_dir / "data" / "staging" / staging_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    filename = Path(result_path).name
    safe_name = sanitize_filename(filename)
    dest = staging_dir / safe_name
    _shutil.copy2(result_path, str(dest))

    # Resolve namespace from sender (mirrors router_sensor logic)
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
        "source": "telegram",
    }
    item_id = write_item(
        intent="drop_file", raw_message="pdf save",
        sender=sender_id, channel_id=chat_id,
        source="telegram", payload=payload,
    )
    update_status(item_id, "confirmed")

    tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
    return f"📎 Saved → {safe_name} {tag_str} `#{item_id[:8]}`"


def _handle_pdf_command(chat_id: str, message_id: int, text: str,
                        media_path: "str | None", media_filename: str = "") -> bool:
    """Handle /pdf (and p shortcut) commands.

    Returns True if the command was handled (caller should skip router dispatch).
    Returns False if this is not a PDF command.
    """
    import re as _re

    # Normalise: expand single-letter alias "p " → "/pdf "
    t = text.strip()
    if _re.match(r"^p\s", t, _re.IGNORECASE):
        t = "/pdf " + t[2:].strip()

    if not _re.match(r"^/pdf\b", t, _re.IGNORECASE):
        return False

    # Parse command and optional argument
    # /pdf [command] [args...]
    parts = t.split(None, 2)  # ["/pdf", command?, args?]
    command = parts[1].lower() if len(parts) > 1 else "help"
    args = parts[2].strip() if len(parts) > 2 else ""

    import re as _re2

    # Parse save flag and optional tags: "save" or "save #receipts #work"
    _save_match = _re2.search(r'\bsave(?:\s+((?:#\w+\s*)+))?\b', args, _re2.I)
    save_mode = bool(_save_match)
    save_tags = []
    if _save_match and _save_match.group(1):
        save_tags = [t.lstrip("#") for t in _save_match.group(1).split()]
    # Strip save clause from args so spec parsers don't see it
    args_clean = _re2.sub(r'\bsave(?:\s+(?:#\w+\s*)+)?\b', '', args, flags=_re2.I).strip()

    # Detect trailing `ocr` chain keyword: runs OCR on every PDF the primary
    # command would deliver. Strip from args so subcommand parsers don't see it.
    ocr_chain = bool(_re2.search(r'\bocr\b', args_clean, _re2.I))
    if ocr_chain:
        args_clean = _re2.sub(r'\bocr\b', '', args_clean, flags=_re2.I).strip()

    _log.info("pdf_cmd chat=%s cmd=%r args=%r save=%s tags=%s ocr=%s media=%s",
              chat_id, command, args_clean, save_mode, save_tags, ocr_chain, media_path)

    def _deliver(file_path: str, caption: str, tags: list = None) -> None:
        """Send file back to Telegram OR stage it for vault via /drop.
        If `ocr` was requested AND the file is a PDF, runs OCR after delivery
        and sends the resulting .txt as a follow-up document.
        """
        if save_mode:
            t = tags or save_tags or []
            msg = _pdf_drop_file(file_path, t, sender_id=chat_id,
                                 chat_id=chat_id, message_id=message_id)
            _send_reply(chat_id, msg, reply_to=message_id)
        else:
            _send_document(chat_id, file_path, caption=caption, reply_to=message_id)

        if ocr_chain and file_path.lower().endswith(".pdf"):
            try:
                r = _pdf_ocr(file_path)
                _send_document(chat_id, r["text_file"],
                               caption=f"OCR: {r['ocr_pages']}/{r['pages']} pages, "
                                       f"{r['chars']} chars",
                               reply_to=message_id)
            except Exception as e:
                _send_reply(chat_id, f"⚠️ OCR failed: {e}", reply_to=message_id)

    try:
        sys.path.insert(0, str(BASE))
        from tools.pdf_tool import (
            compress, extract, split, merge, delete_pages, delete_blank_pages,
            ocr as _pdf_ocr, help_text,
        )

        _pdf_out = _MEDIA_DIR / "pdf_output"
        _pdf_out.mkdir(parents=True, exist_ok=True)

        def _require_media() -> str:
            if not media_path or not Path(media_path).exists():
                raise ValueError("Please attach a PDF file to use this command.")
            return media_path

        if command == "help":
            _send_reply(chat_id, help_text(), reply_to=message_id)

        elif command == "compress":
            src = _require_media()
            stem = Path(src).stem
            out = str(_pdf_out / f"{stem}_compressed.pdf")
            q_match = _re2.search(r'\bq(?:uality)?=(\d+)\b', args_clean, _re2.I)
            d_match = _re2.search(r'\bdpi=(\d+)\b', args_clean, _re2.I)
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
            stem = Path(src).stem
            out = str(_pdf_out / f"{stem}_text.txt")
            # Allow `p ocr lang=eng+deu` (rare)
            lang_match = _re2.search(r'\blang(?:uage)?=([\w+]+)', args_clean, _re2.I)
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
                    _pdf_drop_file(path, save_tags, sender_id=chat_id,
                                   chat_id=chat_id, message_id=message_id)
                _send_reply(chat_id,
                    f"📎 Saved {r['count']} parts to vault.",
                    reply_to=message_id)
            else:
                _send_reply(chat_id, f"Split into {r['count']} files — sending…",
                            reply_to=message_id)
                for path in r["outputs"]:
                    _send_document(chat_id, path)

        elif command == "merge":
            now = time.time()
            entries = [
                e for e in _pdf_merge_stage.get(chat_id, [])
                if now - e["ts"] < _PDF_STAGE_TTL and Path(e["path"]).exists()
            ]
            if not entries:
                _send_reply(chat_id,
                    "No files staged for merge.\n"
                    "Send your PDFs/images first, then /pdf merge.",
                    reply_to=message_id)
            else:
                paths = [e["path"] for e in entries]
                stem = Path(paths[0]).stem
                out = str(_pdf_out / f"{stem}_merged.pdf")
                r = merge(paths, out)
                _deliver(r["output"],
                         f"Merged {len(paths)} files → {r['pages']} pages")
                _pdf_merge_stage.pop(chat_id, None)

        elif command == "delete":
            if not args_clean:
                raise ValueError("Usage: /pdf delete <spec>  e.g. /pdf delete 3-5 or /pdf delete 2s")

            # Subcommand: "delete blank-pages [dont-return]"
            first_tok = args_clean.split(None, 1)[0].lower()
            if first_tok in ("blank-pages", "blanks", "blank"):
                rest = args_clean[len(first_tok):].lower()
                dont_return = bool(_re2.search(r'\b(dont[-]?return|no[-]?blanks)\b', rest))
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
                    _deliver(r["blanks_output"],
                             "Removed pages — verify they were blank")
            else:
                src = _require_media()
                stem = Path(src).stem
                out = str(_pdf_out / f"{stem}_trimmed.pdf")
                r = delete_pages(src, args_clean, out)
                _deliver(r["output"],
                         f"Removed {r['removed']} pages, {r['remaining']} remaining")

        else:
            _send_reply(chat_id,
                f"Unknown PDF command: {command!r}\n\n" + help_text(),
                reply_to=message_id)

    except (ValueError, FileNotFoundError) as e:
        _send_reply(chat_id, f"⚠️ {e}", reply_to=message_id)
    except ImportError:
        _send_reply(chat_id, "⚠️ pymupdf not installed on this node.", reply_to=message_id)
    except Exception as e:
        _log.error("pdf_cmd error: %s", e, exc_info=True)
        _send_reply(chat_id, f"⚠️ PDF error: {e}", reply_to=message_id)

    return True


# ── Per-update handler ─────────────────────────────────────────────────────────

def _answer_callback(callback_id: str, text: str = "") -> None:
    """Dismiss the loading spinner on an inline button press."""
    _tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text}, timeout=5)


def _handle_update(update: dict) -> None:
    # ── Inline button press (callback_query) ──────────────────────────────────
    cb = update.get("callback_query")
    if cb:
        cb_id = cb.get("id", "")
        data = (cb.get("data") or "").strip()
        user_id = str(cb.get("from", {}).get("id", ""))
        msg = cb.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = int(msg.get("message_id", 0))
        _answer_callback(cb_id)  # dismiss spinner immediately
        if data:
            _log.info("callback sender=%s chat=%s data=%.60r", user_id, chat_id, data)
            raw_input = _build_format_a(user_id, chat_id, message_id, data)
            try:
                result = subprocess.run(
                    [sys.executable, str(BASE / "sensor" / "router_sensor.py"), raw_input],
                    capture_output=True, text=True, timeout=90, env=os.environ.copy(),
                )
                reply = result.stdout.strip()
                if reply:
                    _send_reply(chat_id, reply)
            except Exception as e:
                _log.error("callback dispatch error: %s", e)
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Check for media attachments (document, photo, video, audio, voice)
    has_media = any(msg.get(k) for k in ("document", "photo", "video", "audio", "voice"))

    # Drop messages with no text AND no media
    if not text and not has_media:
        return

    sender_id = str(msg.get("from", {}).get("id", ""))
    chat_id = str(msg.get("chat", {}).get("id", ""))
    message_id = int(msg.get("message_id", 0))

    # Download media if present
    media_path = media_mime = media_filename = None
    if has_media:
        media_info = _extract_media(msg)
        if media_info:
            media_path, media_mime, media_filename = media_info

    # Stage PDF/image files for potential /pdf merge use
    if media_path and media_mime and media_filename:
        _stage_for_pdf_merge(chat_id, media_path, media_filename, media_mime)

    # Intercept /pdf commands (and single-letter alias "p ") before the router.
    # PDF ops need to send files back via sendDocument — not possible from the
    # router subprocess which communicates via stdout text only.
    import re as _re
    _is_pdf_cmd = bool(
        _re.match(r"^/pdf\b", text, _re.IGNORECASE)
        or _re.match(r"^p\s", text, _re.IGNORECASE)
    )
    if _is_pdf_cmd:
        _handle_pdf_command(chat_id, message_id, text, media_path,
                            media_filename or "")
        return

    # If there's media but no text, signal the router to ask the user about filing
    if not text and media_path:
        text = "__drop_ask__"

    # Early blocked-sender check — avoids spawning the router subprocess
    try:
        from gateway.ingress import is_blocked as _is_blocked
        if _is_blocked(sender_id):
            _log.info("drop sender=%s (blocked)", sender_id)
            return
    except Exception:
        pass  # non-fatal — router_sensor will re-check

    raw_input = _build_format_a(sender_id, chat_id, message_id, text,
                                media_path=media_path, mime_type=media_mime)

    _log.info("dispatch sender=%s chat=%s msg=%d text=%.60r",
              sender_id, chat_id, message_id, text)

    try:
        result = subprocess.run(
            [sys.executable, str(BASE / "sensor" / "router_sensor.py"), raw_input],
            capture_output=True,
            text=True,
            timeout=90,
            env=os.environ.copy(),
        )
        if result.stderr:
            _log.debug("stderr: %.300s", result.stderr.strip())
        reply = result.stdout.strip()
        if reply:
            _log.info("reply %.80r → chat %s", reply, chat_id)
            _send_reply(chat_id, reply, reply_to=message_id)
    except subprocess.TimeoutExpired:
        _log.error("router_sensor timed out for %.40r", text)
    except Exception as e:
        _log.error("dispatch error: %s", e)


# ── Main poll loop ─────────────────────────────────────────────────────────────

_MAX_POLL_BACKOFF = 300  # seconds


def _poll_backoff(consecutive: int) -> None:
    """Exponential backoff: 5s, 10s, 20s, 40s, 80s, 160s, 300s max."""
    delay = min(5 * (2 ** min(consecutive - 1, 5)), _MAX_POLL_BACKOFF)
    _log.info("poll error #%d, backing off %ds", consecutive, delay)
    time.sleep(delay)


def main() -> None:
    if not BOT_TOKEN:
        _log.error("TELEGRAM_BOT_TOKEN not set — exiting")
        sys.exit(1)

    offset = _load_offset()
    _log.info("starting poll loop at offset=%d", offset)

    consecutive_errors = 0

    while True:
        try:
            resp = _tg(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message", "edited_message", "callback_query"],
                },
                timeout=POLL_TIMEOUT + 10,
            )
            updates = resp.get("result")
            if updates is None:
                # _tg() returned {} — request failed; back off before retrying
                consecutive_errors += 1
                _poll_backoff(consecutive_errors)
                continue
            consecutive_errors = 0
            for upd in updates:
                try:
                    _handle_update(upd)
                except Exception as e:
                    _log.error("handle_update failed: %s", e)
                uid = upd.get("update_id", 0)
                if uid >= offset:
                    offset = uid + 1
                    _save_offset(uid)

        except KeyboardInterrupt:
            _log.info("shutting down")
            break
        except Exception as e:
            consecutive_errors += 1
            _log.error("poll loop error: %s", e)
            _poll_backoff(consecutive_errors)


if __name__ == "__main__":
    main()
