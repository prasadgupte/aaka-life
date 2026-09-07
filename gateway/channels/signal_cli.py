"""
gateway/channels/signal_cli.py — Signal channel adapter (outbound).

Named `signal_cli`, NOT `signal`: a module called `signal.py` inside
gateway/channels/ shadows the stdlib `signal` module for anything that runs with
that directory on sys.path (pytest, `python3 gateway/channels/foo.py`). The
channel *name* string stays "signal" everywhere else.

Talks to a local `signal-cli` daemon over its JSON-RPC HTTP endpoint:

    signal-cli -a +49170... daemon --http 127.0.0.1:18794

Endpoints (signal-cli man page `signal-cli-jsonrpc.5`):
    POST /api/v1/rpc      single or batch JSON-RPC 2.0 request
    GET  /api/v1/events   Server-Sent Events stream of incoming messages
    GET  /api/v1/check    200 OK while the daemon is alive

egress.py dispatches send_text / send_photo / send_document / send_reaction here
exactly like the Telegram and WhatsApp adapters. stdlib only.

Recipient forms (OutboundMessage.recipient):
    "+49170..."           E.164 phone      → JSON-RPC  recipient=["+49170…"]
    "<uuid>"              Signal ACI uuid  → JSON-RPC  recipient=["<uuid>"]
    "group:<groupId>"     Signal group     → JSON-RPC  groupId=<base64 id>

Signal has no inline keyboards. A `reply_markup` (Telegram InlineKeyboardMarkup
shape, built by gateway/agent_api.py::_build_reply_markup and the router's
confirm flows) is rendered as a numbered option list appended to the text, and
the option → callback_data map is remembered per chat in
$AAKA_CONFIG_DIR/data/signal_options.json so sensor/signal_poller.py can turn a
bare "2" reply into the matching callback_data — the same value a Telegram
button tap would have delivered. The poller and this adapter must therefore run
on the same host (they share that file), which they do: both sit beside the
signal-cli daemon on the Mac.

Env:
    SIGNAL_CLI_URL   base URL of the daemon (default http://127.0.0.1:18794)
    SIGNAL_ACCOUNT   aaka's own Signal number (+E.164); optional when the daemon
                     was started with a single -a account, required for a
                     multi-account daemon.

Status: implemented and unit-tested against a mock daemon that speaks the
JSON-RPC + SSE contract above. NOT yet verified against a live Signal account.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from gateway.types import OutboundMessage

GROUP_PREFIX = "group:"

# Signal's own client caps a plain text body at ~2000 characters; longer bodies
# get turned into a "long message" attachment by the official clients. Splitting
# ourselves keeps every chunk a normal, quotable message (same idea as
# telegram.py::_split_text, different limit).
_MAX_LEN = 1800

# How long a rendered option list stays resolvable for a numeric reply.
_OPTIONS_TTL_S = 6 * 3600


# ── Config ────────────────────────────────────────────────────────────────────

def _base_url() -> str:
    return os.environ.get("SIGNAL_CLI_URL", "http://127.0.0.1:18794").rstrip("/")


def _account() -> str:
    return os.environ.get("SIGNAL_ACCOUNT", "").strip()


def _config_dir() -> Path:
    return Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))


# ── JSON-RPC transport ────────────────────────────────────────────────────────

def rpc(method: str, params: "dict | None" = None, timeout: int = 20) -> dict:
    """One JSON-RPC 2.0 call against the daemon. Returns the `result` object
    (wrapped in {"result": …} when the daemon returns a scalar).

    Raises RuntimeError on transport errors and on JSON-RPC `error` responses —
    egress.send() logs the failure to egress.jsonl and re-raises, same as every
    other channel.
    """
    params = dict(params or {})
    if _account() and "account" not in params:
        params["account"] = _account()
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": str(int(time.time() * 1000)),
        "method": method,
        "params": params,
    }).encode()
    req = urllib.request.Request(
        f"{_base_url()}/api/v1/rpc", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"signal-cli HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"signal-cli unreachable at {_base_url()}: {exc}") from exc

    data = json.loads(raw) if raw else {}
    if isinstance(data, list):            # batch response — we only ever send one
        data = data[0] if data else {}
    if data.get("error"):
        err = data["error"] or {}
        msg = err.get("message") if isinstance(err, dict) else err
        raise RuntimeError(f"signal-cli rpc {method}: {msg}")
    res = data.get("result")
    return res if isinstance(res, dict) else {"result": res}


# ── Addressing ────────────────────────────────────────────────────────────────

def is_group(recipient: str) -> bool:
    return str(recipient or "").startswith(GROUP_PREFIX)


def group_id(recipient: str) -> str:
    """Bare base64 groupId for a "group:<id>" recipient ("" when it is a DM)."""
    return str(recipient)[len(GROUP_PREFIX):] if is_group(recipient) else ""


def _target(recipient: str) -> dict:
    """JSON-RPC addressing params for a recipient string."""
    if is_group(recipient):
        return {"groupId": group_id(recipient)}
    return {"recipient": [str(recipient)]}


# ── Option rendering (Telegram inline keyboard → numbered list) ───────────────

def _options_from_markup(markup: "dict | None") -> "list[tuple[str, str]]":
    """Flatten an InlineKeyboardMarkup into [(label, callback_data), ...]."""
    out: "list[tuple[str, str]]" = []
    if not isinstance(markup, dict):
        return out
    for row in markup.get("inline_keyboard") or []:
        for btn in row or []:
            if not isinstance(btn, dict):
                continue
            label = str(btn.get("text") or "").strip()
            data = str(btn.get("callback_data") or btn.get("url") or label).strip()
            if label:
                out.append((label, data))
    return out


def _options_path() -> Path:
    return _config_dir() / "data" / "signal_options.json"


def _load_options() -> dict:
    try:
        p = _options_path()
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _remember_options(chat: str, options: "list[tuple[str, str]]") -> None:
    store = _load_options()
    now = int(time.time())
    # Drop stale entries first so the file never grows unbounded.
    store = {k: v for k, v in store.items()
             if isinstance(v, dict) and now - int(v.get("ts", 0)) < _OPTIONS_TTL_S}
    store[str(chat)] = {"ts": now, "options": [d for _, d in options]}
    try:
        p = _options_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(store))
    except Exception:
        pass  # best-effort: losing the map only degrades numeric replies


def resolve_option(chat: str, text: str) -> "str | None":
    """If `text` is a bare number matching an option list recently sent to
    `chat`, return that option's callback_data; else None.

    Consumes the entry on a match, so one rendered keyboard answers exactly once
    — mirroring Telegram, where a tapped inline keyboard is answered once and the
    router's pending-confirm state moves on.
    """
    t = str(text or "").strip().rstrip(".)")
    if not t.isdigit():
        return None
    store = _load_options()
    entry = store.get(str(chat))
    if not isinstance(entry, dict):
        return None
    if int(time.time()) - int(entry.get("ts", 0)) >= _OPTIONS_TTL_S:
        return None
    opts = entry.get("options") or []
    idx = int(t) - 1
    if not (0 <= idx < len(opts)):
        return None
    try:
        del store[str(chat)]
        _options_path().write_text(json.dumps(store))
    except Exception:
        pass
    return str(opts[idx])


def render_options(text: str, markup: "dict | None", chat: str) -> str:
    """Append a numbered option list to `text` and remember label→callback_data
    for `chat`. Returns `text` unchanged when there is no keyboard."""
    opts = _options_from_markup(markup)
    if not opts:
        return text
    _remember_options(chat, opts)
    lines = [f"{i}. {label}" for i, (label, _) in enumerate(opts, 1)]
    body = "\n".join(lines)
    return f"{text}\n\nReply with a number:\n{body}" if text else f"Reply with a number:\n{body}"


# ── Text splitting ────────────────────────────────────────────────────────────

def _split_text(text: str) -> "list[str]":
    if len(text) <= _MAX_LEN:
        return [text]
    chunks: "list[str]" = []
    while text:
        if len(text) <= _MAX_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, _MAX_LEN)
        if split_at <= 0:
            split_at = _MAX_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ── egress contract ───────────────────────────────────────────────────────────

def _quote_params(msg: OutboundMessage) -> dict:
    """Build quoteTimestamp/quoteAuthor from reply_to_message_id.

    `reply_to_message_id` carries the Signal message timestamp (the poller sets
    it from envelope.timestamp), which is what quoteTimestamp expects. Quoting a
    message in a *group* also needs its author, so the poller uses the same
    "<author>:<timestamp>" shape it uses for reactions; a bare timestamp (what
    flush_outbox and the agent API pass) quotes fine in a DM.
    """
    rid = str(msg.reply_to_message_id or "")
    author, _, ts = rid.rpartition(":")
    if not ts.isdigit():
        return {}
    params: dict = {"quoteTimestamp": int(ts)}
    if author:
        params["quoteAuthor"] = author
    elif is_group(msg.recipient):
        # A group quote without an author is rejected by signal-cli — send the
        # message unquoted rather than failing the whole delivery.
        return {}
    return params


def send_text(msg: OutboundMessage) -> None:
    text = render_options(msg.text or "", msg.reply_markup, msg.recipient)
    chunks = _split_text(text)
    for i, chunk in enumerate(chunks):
        params = {"message": chunk, **_target(msg.recipient)}
        if i == 0:
            params.update(_quote_params(msg))
        rpc("send", params)


def send_photo(msg: OutboundMessage) -> None:
    # signal-cli takes attachment *paths*; stage the bytes in a temp file.
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
        fh.write(msg.photo_bytes or b"")
        tmp = fh.name
    try:
        rpc("send", {
            "message": msg.photo_caption or "",
            "attachments": [tmp],
            **_target(msg.recipient),
            **_quote_params(msg),
        }, timeout=60)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def send_document(msg: OutboundMessage) -> None:
    path = msg.file_path or ""
    if not path or not Path(path).is_file():
        raise RuntimeError(f"signal send_document: file not found: {path!r}")
    rpc("send", {
        "message": msg.file_caption or "",
        "attachments": [path],
        **_target(msg.recipient),
        **_quote_params(msg),
    }, timeout=120)


def send_reaction(msg: OutboundMessage) -> None:
    """React to a message.

    `reaction_message_id` is either "<author>:<timestamp>" (what the poller
    emits — the author is required for group reactions) or a bare "<timestamp>",
    in which case the author is the DM recipient.
    """
    rid = str(msg.reaction_message_id or "")
    author, _, ts = rid.rpartition(":")
    if not ts.isdigit():
        return None  # nothing addressable to react to — stay quiet
    if not author and not is_group(msg.recipient):
        author = str(msg.recipient)
    if not author:
        return None  # group reaction without a target author is not sendable
    rpc("sendReaction", {
        "emoji": msg.emoji or "👍",
        "targetAuthor": author,
        "targetTimestamp": int(ts),
        **_target(msg.recipient),
    }, timeout=10)


# ── Health (used by admin/setup_check.py + admin/diagnose.sh) ──────────────────

def status(timeout: int = 3) -> dict:
    """{"ok": bool, "version": str, "account": str, "error": str}"""
    try:
        res = rpc("version", {}, timeout=timeout)
        ver = res.get("version") or res.get("result") or ""
        return {"ok": True, "version": str(ver), "account": _account(), "error": ""}
    except Exception as exc:
        return {"ok": False, "version": "", "account": _account(), "error": str(exc)}


def list_groups(timeout: int = 10) -> list:
    """`listGroups` result — handy for finding a groupId for SIGNAL_GROUP_ID."""
    res = rpc("listGroups", {}, timeout=timeout)
    out = res.get("result", res)
    return out if isinstance(out, list) else []
