# Implementation Plan: wa-sidecar

**Branch:** `feature/wa-sidecar`  
**PRD:** `features/wa-sidecar/prd.md` (20 ACs across 4 stories)  
**Deliverable scope:** `wa-sidecar/` Node service + `sensor/wa_inbound.py` receiver + rewired `gateway/channels/whatsapp.py` + config extensions. No feature code in this file — build order only.

---

## Grounding: key signatures found

### gateway/ingress.py — entry point

```python
from gateway.ingress import receive, InboundMessage, Channel

parsed = receive(InboundMessage(
    raw_text="hello",          # text extracted by the receiver — Format C (plain)
    sender_id="...",           # e.g. "4915123456789@s.whatsapp.net"
    channel=Channel.WHATSAPP,  # "whatsapp" string also accepted
    channel_id="...",          # same as sender_id for DMs
    message_id="...",          # Baileys key.id
    media_path=None,           # set for media messages
    mime_type=None,
    timestamp="2026-09-01T10:00:00Z",
    source="wa_inbound",
))
# returns ParsedMessage | None (None = blocked or parse error)
```

`normalize()` falls through to **Format C** when `raw_text` is plain text (no Format A
or B envelope). `sender_id` and `channel_id` are taken directly from `InboundMessage`
fields — no new normalization branch needed. `is_self_dm` stays `False` in Format C;
the receiver sets it via `from_me` in the POST body (handled by mapping it onto the
`ParsedMessage.is_self_dm` field — see Task 2 below).

### gateway/channels/whatsapp.py — interface to preserve (unchanged signatures)

```python
def send_text(msg: OutboundMessage) -> None          # msg.recipient = JID
def send_photo(msg: OutboundMessage) -> None         # msg.photo_bytes, msg.photo_caption
def send_document(msg: OutboundMessage) -> None      # msg.file_path, msg.file_caption
def send_reaction(msg: OutboundMessage) -> None      # msg.reaction_message_id, msg.emoji
```

Currently delegates to `OpenClawBackend` via `subprocess.run`. Task 3 replaces the body
with HTTP calls; signatures are identical.

### gateway/config.py — where new vars land

```python
ENABLED_CHANNELS: list  # "whatsapp" added here to activate WA path
```

Two new vars to add:
- `WA_SIDECAR_PORT` (default `18792`) — sidecar HTTP API
- `WA_RECEIVER_PORT` (default `18793`) — Python inbound receiver
- `WA_AUTH_DIR` (default `$AAKA_CONFIG_DIR/whatsapp-auth`) — only read by sidecar, surfaced in config for diagnose.sh

### gateway/backends/openclaw.py — not touched

The file stays. Only `gateway/channels/whatsapp.py` stops importing it. The `grep` AC
checks `whatsapp.py` directly, not `backends/`.

### gateway/egress.py — not touched

`_CHANNEL_DISPATCH["whatsapp"]` already lazy-imports `channels/whatsapp.py`. No edits needed.

### wa-backup/server.js patterns to lift

- `makeWASocket + useMultiFileAuthState + fetchLatestBaileysVersion` (lines 352–395)
- `connection.update` handler: `qr` → `QRCode.toDataURL`; `connection === 'open'` sets status;
  `connection === 'close'` checks `DisconnectReason.loggedOut` (401) and `connectionReplaced` (440) — line 467–484
- `messages.upsert` fires on new messages — line 533
- Text extraction: `msg.message?.conversation ?? msg.message?.extendedTextMessage?.text` — line 321

**Strip entirely from wa-backup:** SQLite, Socket.io, multi-account logic, sync history, `messaging-history.set`, media progress tracking.

### Inbound receiver placement

The receiver is a **new standalone file `sensor/wa_inbound.py`** (not merged into the
console server). Rationale:
- It runs on Mac alongside the executor (same process context as `queue_worker.py`, which is where `gateway.ingress` + `aaka_queue` are used).
- The console server (port 8003, launchd `com.aaka.console`) is already running there, but adding a route to it would couple the WA receiver lifetime to the UI. A separate file on port 18793 is simpler to manage, matches the telegram_poller pattern (a standalone loop), and is gated by `whatsapp ∈ ENABLED_CHANNELS`.
- It does NOT import `router_sensor.py` or run on the VPS sensor. The receiver calls `ingress.receive()` then `write_item()` directly (same as `gmail_poller.py` does for email intents, lines 113–141 of `sensor/gmail_poller.py`).

### Port allocation

| Port | Service |
|------|---------|
| 18792 | `wa-sidecar` Node HTTP API (WA_SIDECAR_PORT) |
| 18793 | Python inbound receiver (WA_RECEIVER_PORT) |

Both are unallocated per the CLAUDE.md port registry (registry ends at 18790).

---

## Task List (build order, one commit each)

### Task 1 — `wa-sidecar/` Node service scaffold

**Files:** `wa-sidecar/package.json`, `wa-sidecar/index.js`

**package.json** — minimal, CommonJS (matching wa-backup):
```json
{
  "name": "wa-sidecar",
  "version": "1.0.0",
  "type": "commonjs",
  "main": "index.js",
  "dependencies": {
    "@whiskeysockets/baileys": "7.0.0-rc.9",
    "qrcode": "^1.5.4"
  },
  "engines": { "node": ">=20" }
}
```
Pin Baileys to exact version matching wa-backup; no pino (use `console.log`); no
express dependency — use Node's `http` stdlib. Only two runtime deps.

**index.js structure:**

```
Env: WA_AUTH_DIR (default $AAKA_CONFIG_DIR/whatsapp-auth or ./whatsapp-auth)
     WA_SIDECAR_PORT (default 18792)
     WA_RECEIVER_URL (default http://127.0.0.1:18793/inbound)

State:
  let sock = null
  let currentQrDataUri = null
  let connectionStatus = "connecting"   // "connecting" | "connected" | "qr"
  let connectedJid = null
  let reconnectAttempt = 0

startSession():
  - fs.mkdirSync(WA_AUTH_DIR, { recursive: true })
  - useMultiFileAuthState(WA_AUTH_DIR) → { state, saveCreds }
  - fetchLatestBaileysVersion()
  - makeWASocket({ version, auth: state, logger: pino-silent, browser: Browsers.macOS('Desktop') })
  - sock.ev.on('creds.update', saveCreds)
  - sock.ev.on('connection.update', handleConnectionUpdate)
  - sock.ev.on('messages.upsert', handleMessagesUpsert)

handleConnectionUpdate(update):
  if qr:
    currentQrDataUri = await QRCode.toDataURL(qr)
    connectionStatus = "qr"
    reconnectAttempt = 0
  if connection === 'open':
    connectionStatus = "connected"
    connectedJid = normalizeJid(sock.user.id)
    currentQrDataUri = null
    reconnectAttempt = 0
  if connection === 'close':
    code = lastDisconnect?.error?.output?.statusCode
    if code === DisconnectReason.loggedOut (401) || code === 440 (connectionReplaced):
      console.error(`WA session evicted (code ${code}) — exiting`)
      process.exit(1)       // supervisor (launchd) will alert PG
    else:
      connectionStatus = "connecting"
      delay = Math.min(1000 * 2 ** reconnectAttempt, 30000)  // exp backoff cap 30s
      reconnectAttempt++
      setTimeout(startSession, delay)

handleMessagesUpsert({ messages, type }):
  for msg of messages:
    if msg.key.fromMe && type !== 'notify': continue    // skip history
    text = msg.message?.conversation
          ?? msg.message?.extendedTextMessage?.text
          ?? ""
    if !text && no recognized media: continue
    body = {
      sender_id: msg.key.remoteJid,
      channel_id: msg.key.remoteJid,
      message_id: msg.key.id,
      text: text,
      from_me: msg.key.fromMe,
      timestamp: new Date(Number(msg.messageTimestamp) * 1000).toISOString(),
    }
    // media: download via sock.downloadMediaMessage, save to WA_AUTH_DIR/../wa-media/
    // add media_path + mime_type fields to body
    POST WA_RECEIVER_URL with JSON body
    on error: console.error (discard, do not crash)

HTTP server (http.createServer, port WA_SIDECAR_PORT):
  GET /status  → { status: connectionStatus, jid: connectedJid | null }
  GET /qr      → if status==="connected": 409 { error: "already connected" }
                  else: 200 { qr: currentQrDataUri | null }
  POST /send   → { jid, text } → sock.sendMessage(jid, { text }) → { ok: true }
  POST /send-media → { jid, mime_type, file_path, caption }
                  → read file, sock.sendMessage with imageMessage or documentMessage
                  → { ok: true }
  GET /health  → { ok: true }

Startup:
  startSession()
  server.listen(WA_SIDECAR_PORT, '127.0.0.1', () => console.log(`wa-sidecar listening :${WA_SIDECAR_PORT}`))
```

**Commits in this task:**
- `feat(wa-sidecar): scaffold Node package + HTTP skeleton (no Baileys yet)` — package.json + stub index.js with HTTP server + route handlers returning 503
- `feat(wa-sidecar): wire Baileys session, QR, reconnect backoff` — full startSession + connection.update + reconnect logic
- `feat(wa-sidecar): messages.upsert → POST /inbound, /send + /send-media endpoints` — inbound forward + outbound API

(Three small commits for bisectability; could be squashed if preferred.)

---

### Task 2 — Python inbound receiver (`sensor/wa_inbound.py`)

**File:** `sensor/wa_inbound.py` (new)

A small FastAPI app (following the same pattern as `executor/console/server.py`) that:

1. Listens on `WA_RECEIVER_PORT` (default 18793), `127.0.0.1` only.
2. Exposes `POST /inbound` accepting the JSON body from Task 1's sidecar.
3. Calls `gateway.ingress.receive(InboundMessage(...))` with:
   - `raw_text = body["text"]`
   - `sender_id = body["sender_id"]`
   - `channel = Channel.WHATSAPP`
   - `channel_id = body["channel_id"]`
   - `message_id = body.get("message_id")`
   - `media_path = body.get("media_path")`
   - `mime_type = body.get("mime_type")`
   - `timestamp = body.get("timestamp")`
   - `source = "wa_inbound"`
4. After `receive()` returns a `ParsedMessage`, patches `parsed.is_self_dm = body.get("from_me", False)`.
5. Calls `write_item(intent=_route_parsed(parsed), ...)` via the same approach as the sensor's `route()` function — but the cleanest path is to call `sensor.router_sensor.route(parsed.text)` if the router can accept a pre-parsed message, OR write a thin `_inbound_to_queue(parsed)` helper that calls `write_item` with `intent="pending"` and lets the executor dispatch (same as Telegram's path through the queue).

   **Preferred: reuse `router_sensor.route()`** by importing it on the Mac (it imports cleanly — no Docker). The receiver calls:
   ```python
   import sys; sys.path.insert(0, REPO_ROOT)
   from sensor.router_sensor import route as _route
   reply = _route(parsed.text)   # the route() wrapper handles exceptions
   ```
   Then sends the reply back via `gateway.egress.send(OutboundMessage(..., channel="whatsapp", recipient=parsed.channel_id))`.

   This matches how the webui server handles messages: invoke `route()`, get reply string, send via egress. No queue write needed for the inbound→reply path.

6. Exposes `GET /health` → `{"ok": true}` for diagnose.sh.

**Launcher:** `executor/com.aaka.wasidecar_receiver.plist` — launchd agent, same structure as `com.aaka.console.plist`. Only loaded when `ENABLED_CHANNELS` includes `whatsapp`. The plist runs:
```
/Users/Shared/aaka-repo/venv/bin/python3 sensor/wa_inbound.py
  --port 18793 --host 127.0.0.1
```

**Note on `is_self_dm`:** `normalize()` in Format C sets `is_self_dm=False` unconditionally. The receiver patches the field after `receive()` returns (on the `ParsedMessage` object) because `InboundMessage` doesn't carry `from_me`. This is the lightest approach — no change to `ingress.py`.

---

### Task 3 — Rewire `gateway/channels/whatsapp.py`

**File:** `gateway/channels/whatsapp.py` (rewrite)

Replace the `OpenClawBackend` import with an HTTP client that talks to the sidecar. Function signatures are identical:

```python
"""
gateway/channels/whatsapp.py — WhatsApp channel adapter.

Posts to the wa-sidecar HTTP API (wa-sidecar/index.js) instead of
calling the OpenClaw subprocess. Interface is unchanged; egress.py
needs no modification.
"""
from __future__ import annotations
import json
import urllib.request
from gateway.config import WA_SIDECAR_PORT
from gateway.types import OutboundMessage

_SIDECAR_BASE = f"http://127.0.0.1:{WA_SIDECAR_PORT}"

def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{_SIDECAR_BASE}{path}", data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def send_text(msg: OutboundMessage) -> None:
    _post("/send", {"jid": msg.recipient, "text": msg.text})

def send_photo(msg: OutboundMessage) -> None:
    import tempfile, os
    # photo_bytes arrive as bytes; write to a temp file for /send-media
    suffix = ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(msg.photo_bytes or b"")
        tmp = f.name
    try:
        _post("/send-media", {
            "jid": msg.recipient, "mime_type": "image/jpeg",
            "file_path": tmp, "caption": msg.photo_caption or "",
        })
    finally:
        try: os.unlink(tmp)
        except OSError: pass

def send_document(msg: OutboundMessage) -> None:
    _post("/send-media", {
        "jid": msg.recipient, "mime_type": "application/octet-stream",
        "file_path": msg.file_path or "", "caption": msg.file_caption or "",
    })

def send_reaction(msg: OutboundMessage) -> None:
    pass  # WA reactions: implement via sock.sendMessage react when needed
```

**stdlib only** — `urllib.request` is already used throughout the codebase (telegram_poller.py, etc.). No new dependencies.

---

### Task 4 — Config extensions (`gateway/config.py`)

Add three new vars at the bottom of the existing file:

```python
# ── wa-sidecar (Task 4) ───────────────────────────────────────────────────────
# Active only when "whatsapp" ∈ ENABLED_CHANNELS.
# WA_SIDECAR_URL is derived from port; override the whole URL for non-localhost.
WA_SIDECAR_PORT: int = int(os.environ.get("WA_SIDECAR_PORT", "18792"))
WA_RECEIVER_PORT: int = int(os.environ.get("WA_RECEIVER_PORT", "18793"))
WA_AUTH_DIR: str = os.environ.get(
    "WA_AUTH_DIR",
    str(Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "whatsapp-auth"),
)
```

Update CLAUDE.md port registry: add ports 18792 and 18793.

---

### Task 5 — launchd plist for receiver + sidecar start script

**Files:**
- `executor/com.aaka.wasidecar_receiver.plist` (new) — Python receiver on port 18793
- `wa-sidecar/start.sh` (new, optional convenience) — `node wa-sidecar/index.js` with env export

The plist mirrors `com.aaka.console.plist` structure:
- `ProgramArguments`: venv python3, `sensor/wa_inbound.py`
- `WorkingDirectory`: `/Users/Shared/aaka-repo`
- `EnvironmentVariables`: `AAKA_CONFIG_DIR`, `AAKA_BASE`, `ENABLED_CHANNELS=telegram,whatsapp`
- `RunAtLoad`: true, `KeepAlive`: true
- Log to `aaka-repo-config/logs/wa_inbound.log`
- `ThrottleInterval`: 10

The Node sidecar (`wa-sidecar/index.js`) is **not** wrapped in a plist in this task — PG
starts it manually (`node wa-sidecar/index.js`) during development. A `com.aaka.wasidecar.plist`
can be added as a follow-up after the sidecar passes acceptance on a test number.

**Install instructions** (for `docs/install.md` addendum):
```bash
# Install Node deps (one-time)
cd /Users/Shared/aaka-repo/wa-sidecar && npm install

# Set env and start sidecar (test number only — not prod)
export WA_AUTH_DIR=$AAKA_CONFIG_DIR/whatsapp-auth
export WA_SIDECAR_PORT=18792
node wa-sidecar/index.js   # QR appears in terminal; scan with test WhatsApp

# Load receiver plist
sed "s|\${AAKA_BASE}|/Users/Shared/aaka-repo|g" \
  executor/com.aaka.wasidecar_receiver.plist \
  > ~/Library/LaunchAgents/com.aaka.wasidecar_receiver.plist
launchctl load ~/Library/LaunchAgents/com.aaka.wasidecar_receiver.plist
```

---

### Task 6 — Unit tests (`sensor/test_wa_inbound.py`, `gateway/channels/test_whatsapp_channel.py`)

**File 1: `sensor/test_wa_inbound.py`**

Tests the Python inbound receiver without any network. Uses `fastapi.testclient.TestClient`
against the imported `app` (same pattern as `executor/console/test_server.py`).

```python
# Run: /Users/Shared/aaka-repo/venv/bin/python3 sensor/test_wa_inbound.py
# exit 0 = pass

from unittest.mock import patch, MagicMock
from starlette.testclient import TestClient
import sensor.wa_inbound as wa   # imports the FastAPI app

client = TestClient(wa.app)

def test_inbound_calls_ingress_receive():
    """POST /inbound → ingress.receive() called with channel=whatsapp."""
    mock_parsed = MagicMock()
    mock_parsed.text = "/status"
    mock_parsed.sender_id = "4915123@s.whatsapp.net"
    mock_parsed.channel_id = "4915123@s.whatsapp.net"
    mock_parsed.is_self_dm = False

    with patch("sensor.wa_inbound._ingress_receive", return_value=mock_parsed) as mock_recv, \
         patch("sensor.wa_inbound._route", return_value="[aaka] ok"), \
         patch("sensor.wa_inbound._egress_send"):
        r = client.post("/inbound", json={
            "sender_id": "4915123@s.whatsapp.net",
            "channel_id": "4915123@s.whatsapp.net",
            "message_id": "abc123",
            "text": "/status",
            "from_me": False,
            "timestamp": "2026-09-01T10:00:00Z",
        })
    assert r.status_code == 200
    call_args = mock_recv.call_args[0][0]   # first positional arg = InboundMessage
    assert call_args.channel == "whatsapp"
    assert call_args.sender_id == "4915123@s.whatsapp.net"

def test_inbound_blocked_sender_returns_204():
    """If ingress.receive() returns None (blocked), receiver returns 204."""
    with patch("sensor.wa_inbound._ingress_receive", return_value=None):
        r = client.post("/inbound", json={
            "sender_id": "blocked@s.whatsapp.net",
            "channel_id": "blocked@s.whatsapp.net",
            "message_id": "x", "text": "hi", "from_me": False,
            "timestamp": "2026-09-01T10:00:00Z",
        })
    assert r.status_code == 204

def test_from_me_sets_is_self_dm():
    """from_me=True in body → parsed.is_self_dm=True (patched onto ParsedMessage)."""
    mock_parsed = MagicMock()
    mock_parsed.text = "hello"
    mock_parsed.sender_id = "me@s.whatsapp.net"
    mock_parsed.channel_id = "me@s.whatsapp.net"
    with patch("sensor.wa_inbound._ingress_receive", return_value=mock_parsed), \
         patch("sensor.wa_inbound._route", return_value=""), \
         patch("sensor.wa_inbound._egress_send"):
        client.post("/inbound", json={
            "sender_id": "me@s.whatsapp.net", "channel_id": "me@s.whatsapp.net",
            "message_id": "y", "text": "hello", "from_me": True,
            "timestamp": "2026-09-01T10:00:00Z",
        })
    assert mock_parsed.is_self_dm is True

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
```

**File 2: `gateway/channels/test_whatsapp_channel.py`**

Tests the rewired channel adapter without spawning subprocesses or hitting the sidecar.

```python
# Run: /Users/Shared/aaka-repo/venv/bin/python3 gateway/channels/test_whatsapp_channel.py
from unittest.mock import patch, MagicMock, call
import subprocess

from gateway.types import OutboundMessage, MessageKind

def test_send_text_posts_to_sidecar_no_subprocess():
    """send_text() issues exactly one POST /send, zero subprocess.run calls."""
    msg = OutboundMessage(
        kind=MessageKind.TEXT, recipient="4915123@s.whatsapp.net",
        channel="whatsapp", source="test", text="hello",
    )
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"ok": true}'
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_url, \
         patch("subprocess.run") as mock_sub:
        from gateway.channels.whatsapp import send_text
        send_text(msg)

    assert mock_url.call_count == 1
    req = mock_url.call_args[0][0]
    assert req.get_full_url().endswith("/send")
    assert mock_sub.call_count == 0, "subprocess.run was called — openclaw subprocess leaked!"

def test_send_photo_posts_to_send_media():
    """send_photo() POSTs to /send-media with mime_type=image/jpeg."""
    msg = OutboundMessage(
        kind=MessageKind.PHOTO, recipient="4915123@s.whatsapp.net",
        channel="whatsapp", source="test", photo_bytes=b"\xff\xd8", photo_caption="hi",
    )
    mock_response = MagicMock()
    mock_response.read.return_value = b'{"ok": true}'
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_url:
        from gateway.channels.whatsapp import send_photo
        send_photo(msg)

    req = mock_url.call_args[0][0]
    assert req.get_full_url().endswith("/send-media")
    import json
    body = json.loads(req.data)
    assert body["mime_type"] == "image/jpeg"

def test_no_openclaw_import():
    """gateway/channels/whatsapp.py must not import openclaw."""
    import importlib, sys
    # Remove cached module so we get a fresh import
    for k in list(sys.modules.keys()):
        if "whatsapp" in k or "openclaw" in k:
            del sys.modules[k]
    import gateway.channels.whatsapp as wa_mod
    assert "openclaw" not in dir(wa_mod), "openclaw symbol leaked into whatsapp module"
    assert not any("openclaw" in str(v) for v in vars(wa_mod).values() if isinstance(v, type))
```

---

### Task 7 — diagnose.sh + test.sh additions

**`admin/diagnose.sh`** — append after existing checks:
```bash
# ── WA sidecar (if whatsapp ∈ ENABLED_CHANNELS) ─────────────────────────────
header "WA Sidecar (whatsapp channel)"
WA_SIDECAR_PORT="${WA_SIDECAR_PORT:-18792}"
WA_RECEIVER_PORT="${WA_RECEIVER_PORT:-18793}"
if echo "${ENABLED_CHANNELS:-telegram}" | grep -q "whatsapp"; then
    # Check sidecar status
    WA_STATUS=$(curl -sf "http://127.0.0.1:$WA_SIDECAR_PORT/status" 2>/dev/null || echo "")
    if [ -z "$WA_STATUS" ]; then
        fail "wa-sidecar not reachable on port $WA_SIDECAR_PORT"
    elif echo "$WA_STATUS" | grep -q '"status":"connected"'; then
        ok "wa-sidecar: connected"
    elif echo "$WA_STATUS" | grep -q '"status":"qr"'; then
        warn "wa-sidecar: QR pending — scan required"
    else
        warn "wa-sidecar: status=$WA_STATUS"
    fi
    # Check receiver
    REC_STATUS=$(curl -sf "http://127.0.0.1:$WA_RECEIVER_PORT/health" 2>/dev/null || echo "")
    if echo "$REC_STATUS" | grep -q '"ok":true'; then
        ok "wa-sidecar receiver: healthy on port $WA_RECEIVER_PORT"
    else
        fail "wa-sidecar receiver not reachable on port $WA_RECEIVER_PORT"
    fi
    # Verify openclaw is NOT running
    if pgrep -f "openclaw" &>/dev/null; then
        fail "openclaw process is running — should not be when using wa-sidecar"
    else
        ok "openclaw: not running (expected)"
    fi
else
    info "whatsapp not in ENABLED_CHANNELS — wa-sidecar checks skipped"
fi
```

**`admin/test.sh`** — append:
```bash
# ── WA sidecar unit tests ────────────────────────────────────────────────────
header "WA Sidecar — channel adapter unit tests"
check "whatsapp channel: send_text POSTs to sidecar, no subprocess" \
    bash -c "cd '$REPO_DIR' && '$PYTHON' gateway/channels/test_whatsapp_channel.py"

header "WA Sidecar — inbound receiver unit tests"
check "wa_inbound: receive() called with channel=whatsapp" \
    bash -c "cd '$REPO_DIR' && '$PYTHON' sensor/test_wa_inbound.py"

header "WA Sidecar — no openclaw import in whatsapp channel"
if grep -q "openclaw" gateway/channels/whatsapp.py 2>/dev/null; then
    fail "gateway/channels/whatsapp.py still imports openclaw"
    FAIL=$((FAIL + 1))
else
    ok "gateway/channels/whatsapp.py: no openclaw import"
    PASS=$((PASS + 1))
fi

# Node boot smoke test — only if node is available and WA_TEST_BOOT=1
if [ "${WA_TEST_BOOT:-0}" = "1" ] && command -v node &>/dev/null; then
    header "WA Sidecar — Node boot smoke"
    # Start sidecar briefly, curl /status, kill it
    WA_SIDECAR_PORT=18799 node wa-sidecar/index.js &>/tmp/wa-sidecar-test.log &
    WA_PID=$!
    sleep 3
    WA_BOOT_STATUS=$(curl -sf "http://127.0.0.1:18799/status" 2>/dev/null || echo "")
    kill $WA_PID 2>/dev/null; wait $WA_PID 2>/dev/null || true
    if echo "$WA_BOOT_STATUS" | grep -qE '"status":"(connecting|qr)"'; then
        ok "wa-sidecar Node boot: /status returns connecting|qr"
        PASS=$((PASS + 1))
    else
        fail "wa-sidecar Node boot: /status not reachable or unexpected: $WA_BOOT_STATUS"
        FAIL=$((FAIL + 1))
    fi
fi
```

---

## AC → Test Mapping

| AC | Test | Method | File |
|----|------|--------|------|
| **AC-PAIR-1** QR on stdout + GET /qr returns data-URI | Node boot smoke (`WA_TEST_BOOT=1`): `curl /qr` → `{"qr": "data:..."}` | Automated (gated) | `admin/test.sh` `WA_TEST_BOOT=1` block |
| **AC-PAIR-2** GET /status → connected after scan | Manual (needs live phone scan) — `curl /status` → `{"status":"connected","jid":"..."}` | **Manual** `TEST_LIVE=1` | — |
| **AC-PAIR-3** GET /qr → 409 when connected | Manual — part of T5 | **Manual** `TEST_LIVE=1` | — |
| **AC-PAIR-4** `useMultiFileAuthState` writes to `$WA_AUTH_DIR`, created on startup | Node boot smoke: check dir created; also: grep `WA_AUTH_DIR` in index.js | grep check in `test.sh` | `admin/test.sh` |
| **AC-PAIR-5** No openclaw process | `pgrep openclaw` non-zero while sidecar handles traffic | `admin/diagnose.sh` WA block; part of manual T5 | `admin/diagnose.sh` |
| **AC-IN-1** messages.upsert POSTs to /inbound | Node integration: mock /inbound endpoint, inject synthetic Baileys event — **manual** with test number; unit-verified by reading index.js structure | **Manual** `TEST_LIVE=1`; index.js code review | — |
| **AC-IN-2** wa_inbound.py calls `ingress.receive(channel="whatsapp")` | `test_wa_inbound.py::test_inbound_calls_ingress_receive` | Unit test (mock) | `sensor/test_wa_inbound.py` |
| **AC-IN-3** End-to-end /status intent round-trip | Manual — send `/status` from test WA → check reply | **Manual** `TEST_LIVE=1` | — |
| **AC-IN-4** No new normalization branch in ingress.py | `grep -n "Format D\|wa_inbound\|wa-sidecar" gateway/ingress.py` returns empty | grep check in `test.sh` | `admin/test.sh` |
| **AC-IN-5** `from_me=true` → `is_self_dm=True` | `test_wa_inbound.py::test_from_me_sets_is_self_dm` | Unit test (mock) | `sensor/test_wa_inbound.py` |
| **AC-OUT-1** `whatsapp.py` POSTs to /send, not subprocess | `test_whatsapp_channel.py::test_send_text_posts_to_sidecar_no_subprocess` | Unit test (mock) | `gateway/channels/test_whatsapp_channel.py` |
| **AC-OUT-2** egress.py not modified | `git diff gateway/egress.py` empty on the feature branch | git check (manual / CI) | — |
| **AC-OUT-3** No openclaw import in whatsapp.py | `grep -q "openclaw" gateway/channels/whatsapp.py` → non-zero | `admin/test.sh` grep check | `admin/test.sh` |
| **AC-OUT-4** /send calls `sock.sendMessage(jid, {text})` | Code review of index.js `/send` handler; manual T5 | Code review + **Manual** | — |
| **AC-OUT-5** /send-media selects imageMessage for `image/*` | Code review of index.js `/send-media` handler; `test_whatsapp_channel.py::test_send_photo_posts_to_send_media` confirms mime_type field | Unit test + code review | `gateway/channels/test_whatsapp_channel.py` |
| **AC-OUT-6** `send_text` zero subprocess.run calls | `test_whatsapp_channel.py::test_send_text_posts_to_sidecar_no_subprocess` asserts `mock_sub.call_count == 0` | Unit test (mock) | `gateway/channels/test_whatsapp_channel.py` |
| **AC-SESS-1** Session persists across restart | Manual — kill + restart sidecar, `GET /status` → connected within 15s | **Manual** `TEST_LIVE=1` | — |
| **AC-SESS-2** Reconnect with exp backoff ≤ 30s | Code review of `handleConnectionUpdate` delay = `Math.min(1000 * 2 ** n, 30000)` | Code review | — |
| **AC-SESS-3** loggedOut (401) / connectionReplaced (440) → exit(1) | Code review of index.js: `process.exit(1)` branch; also: `WA_TEST_BOOT=1` smoke confirms sidecar doesn't loop on startup | Code review + boot smoke | `admin/test.sh` |
| **AC-SESS-4** GET /status → "connecting" during reconnect | Node boot smoke: before QR appears, `/status` returns `{"status":"connecting"}` | Node boot smoke | `admin/test.sh` `WA_TEST_BOOT=1` |

### Manual test checklist (`TEST_LIVE=1` — test number only)

Run with a throwaway SIM / secondary WhatsApp number. **Never touch the prod number.**

```
T1: node wa-sidecar/index.js → QR in terminal; curl /qr → data URI; curl /status → "qr"
T2: Scan QR with test phone → curl /status → "connected" within 10s; curl /qr → 409
T3: Send "/status" from test WA → executor health_check reply received in WhatsApp
T4: kill + restart sidecar (keep WA_AUTH_DIR) → /status → "connected" within 15s (no QR)
T5: pgrep openclaw → non-zero exit (not running) during T3 round-trip
T6: send a photo from test WA → wa_inbound.py logs media_path + mime_type
```

---

## Risks and Mitigations

### CRITICAL — WhatsApp session conflict (error 440)

Pairing the sidecar against the **live/prod number** evicts OpenClaw instantly. Error
code 440 = `connectionReplaced`. The sidecar exits with code 1 on this code (AC-SESS-3)
— do not loop. PG must use a throwaway SIM for all testing. The live number is not
touched until OpenClaw is intentionally retired.

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Node runtime pulled into Docker image | None | `wa-sidecar/` is Mac-only. Docker `WITH_WHATSAPP=false` already excludes Node; `wa-sidecar/` is not `COPY`'d in `Dockerfile`. |
| Receiver import of `gateway.ingress` fails | Low | Receiver runs on Mac (same context as queue_worker); test: `python3 -c "from gateway.ingress import receive"` passes in Task 6. |
| Inbound POST delivery failure (receiver not up) | Medium | Sidecar logs and discards on POST error — does not crash. diagnose.sh checks receiver liveness when `whatsapp ∈ ENABLED_CHANNELS`. |
| Baileys API shape changes | Low | Exact version pinned in `package-lock.json`. Do not `npm update` without testing. |
| Port 18792/18793 conflict | None | Both unallocated per port registry. Added to CLAUDE.md in Task 4. |
| Disturbing live sensor/executor | None | `ENABLED_CHANNELS` defaults to `telegram` — WA path is dead unless explicitly opted in. All changes to `whatsapp.py` are in a branch; executor/console/plist unchanged. |
| `wa-sidecar/` checked into the public aaka-life repo | Low | `wa-sidecar/` contains no secrets (auth state lives in `$AAKA_CONFIG_DIR`). `node_modules/` goes in `.gitignore`. |

### How the sidecar is started (gating)

The Node sidecar runs **manually** during v1 development. A launchd plist
(`executor/com.aaka.wasidecar.plist`) can be added in a follow-up PR after the sidecar
passes acceptance on a test number. The Python receiver plist IS provided in Task 5 but
also requires `ENABLED_CHANNELS=telegram,whatsapp` in its environment — it won't load
unless PG opts in.

---

## Files Created / Modified Summary

| File | Status | Task |
|------|--------|------|
| `wa-sidecar/package.json` | New | 1 |
| `wa-sidecar/index.js` | New | 1 |
| `sensor/wa_inbound.py` | New | 2 |
| `executor/com.aaka.wasidecar_receiver.plist` | New | 5 |
| `gateway/channels/whatsapp.py` | Rewrite | 3 |
| `gateway/config.py` | Edit (3 lines added) | 4 |
| `sensor/test_wa_inbound.py` | New | 6 |
| `gateway/channels/test_whatsapp_channel.py` | New | 6 |
| `admin/test.sh` | Append | 7 |
| `admin/diagnose.sh` | Append | 7 |
| `CLAUDE.md` (port table) | Edit | 4 |
| `features/wa-sidecar/plan.md` | This file | — |

**Files explicitly NOT modified:** `gateway/ingress.py`, `gateway/egress.py`,
`gateway/backends/openclaw.py`, `sensor/router_sensor.py`, `executor/queue_worker.py`,
`sensor/Dockerfile`, `docker-compose.prod.yml`.
