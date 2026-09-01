# idea: wa-sidecar (Baileys WhatsApp, OpenClaw-free)

**One line:** A standalone Node/Baileys sidecar that owns the WhatsApp Web session, so aaka's
WhatsApp channel no longer needs OpenClaw. This is the last hard OpenClaw dependency — building it
lets us delete `backends/openclaw.py`, the Dockerfile openclaw install, and the entrypoint daemon.

## Reuse (big head start — do NOT write Baileys from scratch)
`/Users/Shared/tools/wa-backup/` already runs Baileys `^7`:
- `makeWASocket` + `useMultiFileAuthState` (session persistence)
- `connection.update` → QR handling (`QRCode.toDataURL`) + reconnect (`DisconnectReason`)
- `messages.upsert` (inbound), `sock.sendMessage` (outbound)
Lift these patterns (server.js is the fuller reference).

## aaka integration points (already exist — wire into them)
- **Inbound:** `gateway/ingress.py` is the unified inbound entry (`process_inbound`, Channel.WHATSAPP,
  already normalizes WhatsApp JIDs). The sidecar POSTs each incoming WA message to a small Python
  receiver → `gateway.ingress.process_inbound(channel="whatsapp", ...)`. Reuse the WA normalization there.
- **Outbound:** `gateway/channels/whatsapp.py` currently → `OpenClawBackend` (subprocess). Rewrite it to
  HTTP-POST the sidecar's `/send` — keep the SAME `send_text/send_photo/send_document(OutboundMessage)`
  interface so `gateway/egress.py` needs zero changes.

## Components to build
1. **`wa-sidecar/`** (Node): Baileys session; QR pairing (print to terminal + expose as a data-URI /qr
   endpoint); persist auth to `$AAKA_CONFIG_DIR/whatsapp-auth/`; `POST /send` (text) + `/send-media`;
   on inbound → POST to the Python receiver; reconnect on drop; a small package.json (baileys, qrcode).
2. **Python inbound receiver** — a localhost endpoint that accepts the sidecar's inbound POST and calls
   `gateway.ingress.process_inbound(...)`. (Extend the existing sensor/console server, or a tiny receiver.)
3. **Rewire `gateway/channels/whatsapp.py`** → HTTP to the sidecar (drop the OpenClawBackend import).
4. **Config**: `WA_SIDECAR_URL` / port; only active when `whatsapp` ∈ `ENABLED_CHANNELS`.

## Acceptance (test path — PG will pair a test WhatsApp number)
1. Start the sidecar → it prints/serves a QR → PG scans with a **test** WhatsApp → status "connected".
2. **Inbound**: a WhatsApp message to the paired number reaches the router and gets a reply (e.g. `status` → the health reply), with NO OpenClaw process running.
3. **Outbound**: aaka sends a WhatsApp reply through the sidecar (via the unchanged egress path).
4. Session survives a sidecar restart (no re-pair).

## Out of scope (v1)
The full OpenClaw *deletion* (backends/openclaw.py, Dockerfile, entrypoint) is a SEPARATE cleanup task
after this proves out. Group-chat edge cases, media beyond image/pdf, multi-number.

## Constraints
Localhost only; don't disturb the live sensor/executor; test with a throwaway/test WhatsApp number.
Node service is self-contained under `wa-sidecar/` (its own package.json) — no repo-wide Node build.
