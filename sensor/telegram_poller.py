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

# Default AAKA_BASE to this repo (native run); container sets it to /app explicitly.
BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parents[1])
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
# Native runs (direct poller) load .env; via the supervisor the token is already in env.
try:
    from tools.load_env import load_env
    load_env(BASE)
except Exception:
    pass
from sensor import pdf_command  # noqa: E402  (after sys.path setup above)

CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Multi-bot: one poller process per bot, each with its own AAKA_BOT_ID + token.
# Empty = the default/single bot (back-compat).
BOT_ID = os.environ.get("AAKA_BOT_ID", "")

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# Offset persistence — per-bot when AAKA_BOT_ID is set.
_OFFSET_NAME = f"telegram_offset_{BOT_ID}.json" if BOT_ID else "telegram_offset.json"
_OUR_OFFSET_FILE = CONFIG_DIR / "data" / _OFFSET_NAME

POLL_TIMEOUT = 30  # seconds for Telegram long-poll

# ── PDF commands ───────────────────────────────────────────────────────────────
# The /pdf handler is channel-agnostic and lives in sensor/pdf_command.py so the
# Telegram and Signal pollers share one implementation. It is wired up below,
# after _send_reply/_send_document are defined.


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
    """Return the next update_id to request (from our offset file, else 0)."""
    try:
        if _OUR_OFFSET_FILE.exists():
            d = json.loads(_OUR_OFFSET_FILE.read_text())
            uid = int(d.get("lastUpdateId", 0))
            if uid > 0:
                _log.info("loaded offset %d from %s", uid, _OUR_OFFSET_FILE.name)
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

    `text` is user-controlled, so it goes through ingress.neutralize_envelope()
    first — otherwise a message body that starts with its own "Conversation
    info" block or "[media attached: …]" header could be parsed as a second,
    forged envelope downstream (SEC-1 / SEC-3).
    """
    from gateway.ingress import neutralize_envelope as _neutralize
    text = _neutralize(text or "")
    meta = {
        "chat_id": f"telegram:{chat_id}",
        "message_id": str(message_id),
        "sender_id": str(sender_id),
        "conversation_label": f"id:{chat_id}",
    }
    if BOT_ID:
        meta["bot_id"] = BOT_ID  # so replies route back via the same bot
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

_PDF = pdf_command.PdfCommands(
    channel="telegram",
    media_dir=_MEDIA_DIR,
    send_reply=_send_reply,
    send_document=_send_document,
    log=_log,
)


def _stage_for_pdf_merge(chat_id: str, media_path: str, filename: str,
                         mime_type: str) -> None:
    """Record a downloaded file in the per-chat PDF merge stage."""
    _PDF.stage(chat_id, media_path, filename, mime_type)


def _handle_pdf_command(chat_id: str, message_id: int, text: str,
                        media_path: "str | None", media_filename: str = "") -> bool:
    """Handle /pdf (and the `p` shortcut). True = handled, skip router dispatch."""
    return _PDF.handle(chat_id, message_id, text, media_path, media_filename)


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
    if pdf_command.is_pdf_command(text):
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
