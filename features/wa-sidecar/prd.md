# PRD: wa-sidecar — Baileys WhatsApp Sidecar

**Status:** Ready for implementation  
**Track:** OpenClaw removal §5 (`docs/openclaw-removal.md`)  
**Owner:** PG  
**Scope:** `wa-sidecar/` Node service + Python glue. No UI. No React.

---

## 1. Problem & Why Now

WhatsApp is the **only remaining hard OpenClaw dependency**. Every other seam is
already cut:

- LLM calls → `gateway/llm_providers.py` (direct Gemini/Anthropic, Phase 1 done)
- Telegram inbound/outbound → native Python pollers (Phases 2–3 done)
- Container → `WITH_WHATSAPP=false` build strips Node+OpenClaw (Phase 4 written, pending build test)

`gateway/channels/whatsapp.py` still imports `OpenClawBackend` (a subprocess wrapper
that shells out to the `openclaw` CLI). The OpenClaw daemon running inside the sensor
container is the only thing actually holding the WhatsApp Web session and forwarding
inbound messages in Format B.

Building `wa-sidecar` closes this last seam. Once it passes the acceptance tests, the
claim "no open claws" is literally true and the OpenClaw binary can be deleted from the
Dockerfile in a follow-up cleanup.

---

## 2. User Stories & Acceptance Criteria

### Story 1 — Pair (QR)

> As PG, I scan a QR code once to link a WhatsApp number to the sidecar, and the
> sidecar confirms "connected" without me touching any code.

**Acceptance criteria:**

1. **AC-PAIR-1:** `node wa-sidecar/index.js` starts without error, prints a QR code to
   stdout (via the `qrcode` npm package, same pattern as `wa-backup/server.js`
   `QRCode.toDataURL`), and exposes `GET /qr` returning `{"qr": "<data-URI>"}` while
   pairing is in progress.
2. **AC-PAIR-2:** After the user scans with a WhatsApp-linked phone, `GET /status`
   returns `{"status": "connected", "jid": "<e164>@s.whatsapp.net"}` within 10 s.
3. **AC-PAIR-3:** `GET /qr` returns HTTP 409 once connected (QR no longer valid).
4. **AC-PAIR-4:** The pairing flow is driven by `useMultiFileAuthState` writing to
   `$AAKA_CONFIG_DIR/whatsapp-auth/` (not hardcoded, env-configurable via
   `WA_AUTH_DIR`). The directory is created if it does not exist.
5. **AC-PAIR-5:** No `openclaw` process is spawned at any point during pairing or
   normal operation. Verified by `pgrep openclaw` returning non-zero.

---

### Story 2 — Inbound (WA message → router → reply)

> As a family member sending a WhatsApp message, my message reaches the aaka router
> and I get a reply — the same as Telegram, with no OpenClaw running.

**Acceptance criteria:**

6. **AC-IN-1:** When a WhatsApp message arrives on the paired number, the sidecar's
   `messages.upsert` handler fires and POSTs to `http://127.0.0.1:<WA_RECEIVER_PORT>/inbound`
   with a JSON body:
   ```json
   {
     "sender_id": "<e164>@s.whatsapp.net",
     "channel_id": "<jid>",
     "message_id": "<Baileys key.id>",
     "text": "<extracted text>",
     "timestamp": "<ISO8601 UTC>"
   }
   ```
   Media messages (image, document) additionally include `"media_path"` and
   `"mime_type"` after the sidecar saves the file locally.
7. **AC-IN-2:** The Python inbound receiver (`sensor/wa_inbound.py` or equivalent)
   calls `gateway.ingress.receive(InboundMessage(channel=Channel.WHATSAPP, ...))`.
   A unit test with a mocked POST confirms `receive()` is called with
   `channel="whatsapp"` and the correct `sender_id` / `text`.
8. **AC-IN-3:** Sending `/status` from a paired test WhatsApp number reaches the
   executor's `health_check` intent and the executor sends a reply back through the
   sidecar's `POST /send` — end-to-end, no OpenClaw.
9. **AC-IN-4:** The WA JID normalization already in `gateway/ingress.py` (`Format B`
   regex + `is_self_dm`) is reused; the new Format A-style plain payload from
   the sidecar routes through Format C (plain text, sender/channel from
   `InboundMessage` fields) so no new normalization branch is added to `ingress.py`.
10. **AC-IN-5:** Self-DMs (messages from the paired number itself) set `is_self_dm=True`
    via `msg.key.fromMe` in the Baileys event, passed as `"from_me": true` in the
    inbound POST body.

---

### Story 3 — Outbound (aaka → WA)

> As an executor reply, I want aaka to send WhatsApp messages through the sidecar
> using the existing `send_text/send_photo/send_document` interface — no OpenClaw
> subprocess, no changes to `gateway/egress.py`.

**Acceptance criteria:**

11. **AC-OUT-1:** `gateway/channels/whatsapp.py` is rewritten to `POST
    http://127.0.0.1:<WA_SIDECAR_PORT>/send` (text) and `/send-media` (photo/document)
    instead of calling `OpenClawBackend`. The `send_text`, `send_photo`, `send_document`,
    and `send_reaction` function signatures are unchanged.
12. **AC-OUT-2:** `gateway/egress.py` is not modified — it still dispatches to
    `channels/whatsapp.send_text(msg)` etc.
13. **AC-OUT-3:** `gateway/backends/openclaw.py` is **not** imported anywhere in the
    WhatsApp channel path. `grep -r "openclaw" gateway/channels/whatsapp.py` returns
    empty.
14. **AC-OUT-4:** The sidecar's `POST /send` endpoint accepts
    `{"jid": "...", "text": "..."}` and calls `sock.sendMessage(jid,
    {text})` via the live Baileys socket, returning `{"ok": true}` on success.
15. **AC-OUT-5:** The sidecar's `POST /send-media` accepts `{"jid": "...",
    "mime_type": "...", "file_path": "...", "caption": "..."}` and calls
    `sock.sendMessage` with the appropriate Baileys media message type
    (`imageMessage` for `image/*`, `documentMessage` otherwise).
16. **AC-OUT-6:** A mock unit test for `channels/whatsapp.py` confirms that calling
    `send_text(OutboundMessage(...))` issues exactly one `POST /send` HTTP request
    and spawns zero subprocesses (`subprocess.run` call count = 0).

---

### Story 4 — Reconnect / Session Persist

> As an always-on sidecar, I reconnect automatically after network drops and survive
> a process restart without re-pairing.

**Acceptance criteria:**

17. **AC-SESS-1:** `$AAKA_CONFIG_DIR/whatsapp-auth/` (the `useMultiFileAuthState` dir)
    persists across sidecar restarts. After `kill + node wa-sidecar/index.js`, `GET
    /status` returns `{"status": "connected"}` within 15 s **without** a new QR scan.
18. **AC-SESS-2:** On `connection.update` with `DisconnectReason.connectionClosed` or
    `connectionLost`, the sidecar calls `startSession()` again (same retry loop as
    `wa-backup/server.js` `startSession`). Reconnect attempts use exponential back-off
    capped at 30 s (to mirror the sensor's TG backoff posture).
19. **AC-SESS-3:** On `DisconnectReason.loggedOut` (error 401) or `connectionReplaced`
    (error 440 — session stolen), the sidecar does **not** loop; it logs the reason and
    exits with a non-zero status so a supervisor (launchd / Docker restart policy) can
    alert PG.
20. **AC-SESS-4:** `GET /status` returns `{"status": "connecting"}` during a
    reconnect attempt, never hangs indefinitely.

---

## 3. Components

| Component | Location | Language | Purpose |
|-----------|----------|----------|---------|
| Baileys sidecar | `wa-sidecar/index.js` | Node 20 | WA Web session, QR, inbound forward, outbound API |
| Package manifest | `wa-sidecar/package.json` | — | `@whiskeysockets/baileys ^7`, `qrcode`, no other deps |
| Python inbound receiver | `sensor/wa_inbound.py` | Python | Tiny HTTP endpoint; receives sidecar POST → calls `ingress.receive` |
| Rewired WA channel | `gateway/channels/whatsapp.py` | Python | Replace `OpenClawBackend` with HTTP client to sidecar |
| Config | `.env` + `gateway/config.py` | — | `WA_SIDECAR_PORT` (default 18792), `WA_SIDECAR_URL`, `WA_AUTH_DIR` |

**Reuse target:** lift `makeWASocket + useMultiFileAuthState + connection.update +
messages.upsert + sock.sendMessage` patterns from
`/Users/Shared/tools/wa-backup/server.js` (lines 344–500). Strip the SQLite/Socket.io/
multi-account complexity — the sidecar is single-number, stateless (no DB), and has no
browser UI.

---

## 4. Non-Goals (v1)

- **OpenClaw deletion** — `backends/openclaw.py`, Dockerfile `WITH_WHATSAPP` build
  arg, `entrypoint.sh` OpenClaw daemon block. This is a **separate cleanup task**
  after the sidecar passes acceptance tests on a test number.
- Group-chat edge cases (group JIDs, admin/mention events).
- Media types beyond image (`image/*`) and document (`application/pdf` etc.).
- Multi-number / multi-account WhatsApp.
- A browser UI or React frontend.
- Running on the VPS sensor — the sidecar runs on Mac (where `$AAKA_CONFIG_DIR` and
  Google OAuth tokens live). VPS sensor routes WA inbound only when `whatsapp` is in
  `ENABLED_CHANNELS`, which it won't be for default deploys.
- WhatsApp session migration from the existing OpenClaw session — re-scan is expected
  and acceptable for the test number.

---

## 5. Risks

### CRITICAL — WhatsApp Web session conflict (error 440)

WhatsApp Web allows **one active Web session per phone number** at a time. Pairing
the sidecar with the **live/prod number** (the number PG's family uses for aaka) will
instantly evict the existing OpenClaw session and break the production WhatsApp
channel mid-flight.

**Mitigation:** PG must use a **throwaway/test WhatsApp number** (a spare SIM or
a secondary device) for all sidecar development and acceptance testing. The live
number is not touched until the sidecar is proven and the OpenClaw WA path is
intentionally retired. The error 440 path is handled explicitly (AC-SESS-3 above).

### Other risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Baileys API shape changes (`^7` is pinned but upstream moves fast) | Low | Pin to exact version in `package-lock.json`; test before upgrading |
| Inbound POST delivery failure (receiver not up when message arrives) | Medium | Sidecar logs and discards rather than crashing; add diagnose.sh check for receiver liveness |
| `WA_SIDECAR_PORT` conflicts with existing port registry | Low | Default 18792 is unallocated (registry ends at 18790 for agent API); reserve it in `CLAUDE.md` port table |
| Node runtime in `wa-sidecar/` pulled into sensor Docker image | None | `wa-sidecar/` is Mac-only; `WITH_WHATSAPP=false` Dockerfile already excludes Node |
| Python inbound receiver import of `gateway.ingress` fails in sensor (VPS) context | Medium | The receiver runs on Mac alongside the executor; import path identical to `queue_worker.py`; test with `python3 -c "from gateway.ingress import receive"` |

---

## 6. Test Plan

Tests land in `admin/test.sh` (standing rule) and a new `skills/test/twa_sidecar.py` smoke:

| # | Test | Method |
|---|------|--------|
| T1 | sidecar starts, `GET /status` returns valid JSON | `curl` in `test.sh` |
| T2 | `channels/whatsapp.send_text` issues `POST /send`, no subprocess | mock unit test (`unittest.mock`) |
| T3 | inbound receiver calls `ingress.receive` with `channel="whatsapp"` | mock unit test |
| T4 | session persists across sidecar restart (no re-QR) | manual, gated `TEST_LIVE=1` |
| T5 | end-to-end: test WA msg → `/status` intent → reply back | manual, `TEST_LIVE=1`, test number only |
| T6 | `pgrep openclaw` non-zero while sidecar handles a round-trip | part of T5 |

Diagnose check to add to `admin/diagnose.sh`: if `whatsapp` ∈ `ENABLED_CHANNELS`,
verify `GET http://127.0.0.1:$WA_SIDECAR_PORT/status` returns `{"status": "connected"}`.
