#!/usr/bin/env python3
"""
sensor/signal_poller.py — Signal inbound loop (signal-cli JSON-RPC daemon).

The Signal counterpart of sensor/telegram_poller.py. It consumes the daemon's
Server-Sent Events stream, builds the same trusted Format-A envelope the Telegram
poller builds, runs sensor/router_sensor.py as a subprocess, and delivers the
reply through gateway.egress on the "signal" channel.

    signal-cli -a "$SIGNAL_ACCOUNT" daemon --http 127.0.0.1:18794
    python3 sensor/signal_poller.py

Transport (signal-cli man page `signal-cli-jsonrpc.5`):
    GET  /api/v1/events   SSE stream; each event's data is the JSON-RPC
                          notification {"jsonrpc":"2.0","method":"receive",
                          "params":{"envelope":{…},"account":"+…"}}
    POST /api/v1/rpc      used for the `receive` polling fallback and every send
    GET  /api/v1/check    liveness

SIGNAL_POLL_MODE=rpc switches from SSE to polling the `receive` method, for
daemons built without the SSE endpoint.

Envelope handling:
  • dataMessage         → routed (DMs and groups)
  • syncMessage         → routed ONLY for note-to-self (see _sync_self_text)
  • receiptMessage /
    typingMessage / everything else → ignored

Attachments are stored by signal-cli under $SIGNAL_ATTACHMENTS_DIR/<id> with no
extension; we copy each one to $AAKA_CONFIG_DIR/data/signal_media/ under its real
filename so it lands inside an allowed media root (gateway.ingress SEC-3).

Runs on the Mac beside the signal-cli daemon (launchd: com.aaka.signalpoller).
Status: unit-tested against a mock daemon; NOT yet verified against a live
Signal account.
"""

import hashlib
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s signal %(levelname)s %(message)s",
)
_log = logging.getLogger("signal_poller")

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parents[1])
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
try:
    from tools.load_env import load_env
    load_env(BASE)
except Exception:
    pass

from sensor import pdf_command  # noqa: E402

CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
_MEDIA_DIR = CONFIG_DIR / "data" / "signal_media"

POLL_TIMEOUT = 30  # seconds a `receive` RPC waits in rpc fallback mode
_MAX_POLL_BACKOFF = 300


def _base_url() -> str:
    return os.environ.get("SIGNAL_CLI_URL", "http://127.0.0.1:18794").rstrip("/")


def _account() -> str:
    return os.environ.get("SIGNAL_ACCOUNT", "").strip()


def _account_ids() -> set:
    """Every identifier that means "this account": the E.164 number AND the
    account's own UUID.

    Signal addresses recipients by either, and which one an envelope carries is
    not ours to choose — a note-to-self sync from the phone can name the
    destination by uuid alone. Comparing only against the number silently loses
    those, which reads as "aaka ignores me" rather than as a bug.
    """
    ids = {a for a in (_account(),) if a}
    try:
        store = _accounts_store()
        for acct in store.get("accounts") or []:
            num = str(acct.get("number") or "").strip()
            if num and (not _account() or num == _account()):
                ids.add(num)
                uid = str(acct.get("uuid") or "").strip()
                if uid:
                    ids.add(uid)
    except Exception:
        pass
    return ids


def _accounts_store() -> dict:
    """Read signal-cli's account store. Never shell out: signal-cli holds an
    exclusive lock on the account dir while the daemon runs, so any subcommand
    would block indefinitely."""
    env = os.environ.get("SIGNAL_DATA_DIR", "").strip()
    if env:
        root = Path(env)
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        root = (Path(xdg) if xdg else Path.home() / ".local" / "share") / "signal-cli"
    path = root / "data" / "accounts.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _attachments_dir() -> Path:
    env = os.environ.get("SIGNAL_ATTACHMENTS_DIR", "").strip()
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "signal-cli" / "attachments"


# ── Outbound (all through gateway.egress, like every other poller) ────────────

# Fingerprints of text we sent to our own account recently. A note-to-self reply
# comes straight back to us as an inbound self message; without this we would
# answer our own answer forever. (The WhatsApp sidecar does the equivalent
# suppression by message id before it ever POSTs to aaka.)
_recent_self_sends: deque = deque(maxlen=64)


def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode()).hexdigest()[:16]


def _note_recent_self_send(recipient: str, text: str) -> None:
    if recipient and _account() and str(recipient) == _account():
        _recent_self_sends.append(_fingerprint(text))


def _is_echo_of_our_own_reply(text: str) -> bool:
    return _fingerprint(text) in _recent_self_sends


def _send_reply(chat: str, text: str, reply_to=None) -> None:
    if not text or not text.strip():
        return
    from gateway.egress import MessageKind, OutboundMessage, send
    try:
        _note_recent_self_send(chat, text)
        send(OutboundMessage(
            kind=MessageKind.TEXT,
            recipient=str(chat),
            channel="signal",
            text=text,
            source="signal_poller",
            reply_to_message_id=str(reply_to) if reply_to else None,
        ))
    except Exception as exc:
        _log.error("_send_reply egress error: %s", exc)


def _send_document(chat: str, file_path: str, caption: str = "", reply_to=None) -> None:
    from gateway.egress import MessageKind, OutboundMessage, send
    try:
        send(OutboundMessage(
            kind=MessageKind.DOCUMENT,
            recipient=str(chat),
            channel="signal",
            file_path=file_path,
            file_caption=caption,
            source="signal_poller",
            reply_to_message_id=str(reply_to) if reply_to else None,
        ))
    except Exception as exc:
        _log.error("_send_document egress error: %s", exc)


_PDF = pdf_command.PdfCommands(
    channel="signal",
    media_dir=_MEDIA_DIR,
    send_reply=_send_reply,
    send_document=_send_document,
    log=_log,
)


# ── Format A metadata builder ─────────────────────────────────────────────────

def _build_format_a(sender_id: str, chat: str, message_id, text: str,
                    media_path: str = None, mime_type: str = None,
                    sender_name: str = "") -> str:
    """Wrap a Signal message in the trusted Format-A envelope router_sensor.py
    parses (mirrors telegram_poller._build_format_a). The "signal:" chat_id
    prefix is what tells gateway.ingress.normalize() the channel is Signal.

    `text` is user-controlled, so it goes through ingress.neutralize_envelope()
    first — otherwise a body starting with its own "Conversation info" block or
    "[media attached: …]" header could be parsed as a second, forged envelope
    downstream (SEC-1 / SEC-3).
    """
    from gateway.ingress import neutralize_envelope as _neutralize
    text = _neutralize(text or "")
    meta = {
        "chat_id": f"signal:{chat}",
        "message_id": str(message_id),
        "sender_id": str(sender_id),
        "sender_name": str(sender_name or ""),
        "conversation_label": f"id:{chat}",
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


# ── Attachments ───────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    keep = "".join(c for c in str(name) if c.isalnum() or c in "._- ")
    return keep.strip().replace(" ", "_")[:120] or "attachment"


def _copy_attachment(att: dict, timestamp) -> "tuple[str, str, str] | None":
    """Copy one received attachment out of signal-cli's store into
    data/signal_media/. Returns (local_path, mime_type, original_filename)."""
    att_id = str(att.get("id") or "").strip()
    if not att_id:
        return None
    src = _attachments_dir() / att_id
    if not src.is_file():
        _log.warning("attachment %s not found at %s", att_id, src)
        return None
    mime = str(att.get("contentType") or "application/octet-stream")
    filename = str(att.get("filename") or "").strip()
    if not filename:
        ext = mimetypes.guess_extension(mime.split(";")[0].strip()) or ".bin"
        filename = f"{att_id}{ext}"
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = _MEDIA_DIR / f"{timestamp}_{_safe_name(filename)}"
    try:
        shutil.copy2(src, dest)
    except Exception as exc:
        _log.error("attachment copy failed (%s): %s", att_id, exc)
        return None
    _log.info("attachment saved %s (%d bytes)", dest.name, dest.stat().st_size)
    return str(dest), mime, filename


# ── Envelope parsing ──────────────────────────────────────────────────────────

def _sender_of(env: dict) -> str:
    """The handle we use for member lookup and for addressing a DM reply."""
    return str(env.get("sourceNumber") or env.get("sourceUuid") or env.get("source") or "")


def _sync_self_text(env: dict) -> "dict | None":
    """A syncMessage is a copy of something *this account* sent from another
    linked device. We only act on note-to-self (destination == our own account):
    that is the Signal equivalent of the WhatsApp self-chat, and it is the only
    sync destination where replying is wanted. Acting on every syncMessage would
    make aaka barge into every conversation the user has.
    """
    sent = (env.get("syncMessage") or {}).get("sentMessage")
    if not isinstance(sent, dict):
        return None
    ids = _account_ids()
    # Any of the destination spellings may be present; match on all of them.
    dests = {str(sent.get(k) or "").strip()
             for k in ("destinationNumber", "destinationUuid", "destination")}
    dests.discard("")
    if not ids or not (dests & ids):
        return None
    return sent


def _extract(env: dict) -> "dict | None":
    """Normalise one envelope into
    {sender_id, chat, message_id, text, attachments, sender_name, is_self}
    or None when it is not something we route (receipts, typing, empty)."""
    if env.get("receiptMessage") or env.get("typingMessage"):
        return None

    is_self = False
    body = env.get("dataMessage")
    if not isinstance(body, dict):
        body = _sync_self_text(env)
        is_self = body is not None
        if body is None:
            return None

    sender_id = _sender_of(env)
    if not sender_id:
        return None

    group = (body.get("groupInfo") or {}).get("groupId") or ""
    chat = f"group:{group}" if group else sender_id
    timestamp = body.get("timestamp") or env.get("timestamp") or int(time.time() * 1000)

    return {
        "sender_id": sender_id,
        "chat": str(chat),
        "message_id": int(timestamp),
        "text": str(body.get("message") or "").strip(),
        "attachments": body.get("attachments") or [],
        "sender_name": str(env.get("sourceName") or ""),
        "is_self": is_self,
    }


# ── Per-message handler ───────────────────────────────────────────────────────

def _handle_envelope(env: dict) -> None:
    info = _extract(env)
    if not info:
        return

    sender_id = info["sender_id"]
    chat = info["chat"]
    message_id = info["message_id"]
    text = info["text"]

    if info["is_self"] and text and _is_echo_of_our_own_reply(text):
        _log.debug("skip note-to-self echo of our own reply")
        return

    # Media: copy the first attachment into a trusted media root.
    media_path = media_mime = media_filename = None
    for att in info["attachments"]:
        got = _copy_attachment(att, message_id)
        if got:
            media_path, media_mime, media_filename = got
            _PDF.stage(chat, media_path, media_filename, media_mime)
            break

    # A bare "2" answers the numbered option list the outbound adapter rendered
    # in place of a Telegram inline keyboard — resolve it to the callback_data a
    # button tap would have delivered (see gateway/channels/signal_cli.py).
    if text:
        try:
            from gateway.channels.signal_cli import resolve_option
            resolved = resolve_option(chat, text)
            if resolved:
                _log.info("option %s → %.40r", text, resolved)
                text = resolved
        except Exception as exc:
            _log.warning("option resolve failed: %s", exc)

    # /pdf runs in the poller: it has to send files back, which the router
    # subprocess (stdout text only) cannot do.
    if pdf_command.is_pdf_command(text):
        _PDF.handle(chat, message_id, text, media_path, media_filename or "",
                    sender_id=sender_id)
        return

    if not text and media_path:
        text = "__drop_ask__"
    if not text:
        return

    # Early blocked-sender check — avoids spawning the router subprocess.
    try:
        from gateway.ingress import is_blocked as _is_blocked
        if _is_blocked(sender_id):
            _log.info("drop sender=%s (blocked)", sender_id)
            return
    except Exception:
        pass  # non-fatal — router_sensor re-checks

    raw_input = _build_format_a(sender_id, chat, message_id, text,
                                media_path=media_path, mime_type=media_mime,
                                sender_name=info["sender_name"])

    _log.info("dispatch sender=%s chat=%s ts=%s text=%.60r",
              sender_id, chat, message_id, text)
    try:
        result = subprocess.run(
            [sys.executable, str(BASE / "sensor" / "router_sensor.py"), raw_input],
            capture_output=True, text=True, timeout=90, env=os.environ.copy(),
        )
        if result.stderr:
            _log.debug("stderr: %.300s", result.stderr.strip())
        reply = result.stdout.strip()
        if reply:
            _log.info("reply %.80r → %s", reply, chat)
            # "<author>:<timestamp>" so the quote works in groups too, where
            # signal-cli needs quoteAuthor alongside quoteTimestamp.
            _send_reply(chat, reply, reply_to=f"{sender_id}:{message_id}")
    except subprocess.TimeoutExpired:
        _log.error("router_sensor timed out for %.40r", text)
    except Exception as exc:
        _log.error("dispatch error: %s", exc)


def handle_notification(payload: dict) -> None:
    """Route one JSON-RPC `receive` notification (or a bare envelope)."""
    if not isinstance(payload, dict):
        return
    params = payload.get("params")
    if isinstance(params, dict):
        # subscribeReceive wraps the envelope one level deeper.
        inner = params.get("result")
        payload = inner if isinstance(inner, dict) else params
    env = payload.get("envelope") if isinstance(payload, dict) else None
    if isinstance(env, dict):
        _handle_envelope(env)


# ── SSE stream ────────────────────────────────────────────────────────────────

def _iter_sse(timeout: int = 300):
    """Yield parsed `data:` payloads from GET /api/v1/events until the stream
    ends. stdlib only — urlopen gives us a readable socket to consume lines from."""
    req = urllib.request.Request(f"{_base_url()}/api/v1/events",
                                 headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data_lines: list = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":                      # event boundary
                if data_lines:
                    blob = "\n".join(data_lines)
                    data_lines = []
                    try:
                        yield json.loads(blob)
                    except Exception:
                        _log.warning("unparseable SSE data: %.120r", blob)
                continue
            if line.startswith(":"):            # comment / keep-alive
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())


# ── RPC fallback ──────────────────────────────────────────────────────────────

def _receive_rpc() -> list:
    """One `receive` JSON-RPC call → list of notification payloads."""
    from gateway.channels.signal_cli import rpc
    res = rpc("receive", {"timeout": POLL_TIMEOUT}, timeout=POLL_TIMEOUT + 15)
    out = res.get("result", res)
    if isinstance(out, dict):
        out = [out]
    return out if isinstance(out, list) else []


# ── Main loop ─────────────────────────────────────────────────────────────────

def _poll_backoff(consecutive: int) -> None:
    """Exponential backoff: 5s, 10s, 20s, 40s, 80s, 160s, 300s max."""
    delay = min(5 * (2 ** min(consecutive - 1, 5)), _MAX_POLL_BACKOFF)
    _log.info("stream error #%d, backing off %ds", consecutive, delay)
    time.sleep(delay)


def main() -> None:
    mode = os.environ.get("SIGNAL_POLL_MODE", "sse").strip().lower()
    _log.info("starting signal loop mode=%s daemon=%s account=%s",
              mode, _base_url(), _account() or "(daemon default)")

    consecutive_errors = 0
    while True:
        try:
            if mode == "rpc":
                for payload in _receive_rpc():
                    try:
                        handle_notification(payload)
                    except Exception as exc:
                        _log.error("handle failed: %s", exc)
                consecutive_errors = 0
                continue

            for payload in _iter_sse():
                consecutive_errors = 0
                try:
                    handle_notification(payload)
                except Exception as exc:
                    _log.error("handle failed: %s", exc)
            # Stream closed cleanly (daemon restart / idle timeout) — reconnect.
            _log.info("SSE stream closed — reconnecting")
            time.sleep(2)

        except KeyboardInterrupt:
            _log.info("shutting down")
            break
        except (urllib.error.URLError, OSError) as exc:
            consecutive_errors += 1
            _log.error("signal-cli unreachable at %s: %s", _base_url(), exc)
            _poll_backoff(consecutive_errors)
        except Exception as exc:
            consecutive_errors += 1
            _log.error("poll loop error: %s", exc)
            _poll_backoff(consecutive_errors)


if __name__ == "__main__":
    main()
