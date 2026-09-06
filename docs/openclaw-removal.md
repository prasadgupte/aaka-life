# Channel architecture

Aaka once dispatched all outbound sends and LLM calls through OpenClaw, a
subprocess CLI wrapping Telegram, WhatsApp (Baileys), and the LLM call. That
coupling lived in exactly three seams:

| Seam | Then | Now |
|------|------|-----|
| **Telegram** | Routed through OpenClaw's connector | Native — `sensor/telegram_poller.py` (inbound long-poll) + `gateway/channels/telegram.py` (outbound) call `api.telegram.org` directly |
| **LLM calls** | `openclaw agent 'llm'` subprocess | Native — `gateway/llm_providers.py` (gemini · anthropic · claude-cli), selected via `LLM_PROVIDER` |
| **WhatsApp** | `gateway/backends/openclaw.py` subprocess wrapping Baileys | Own Baileys sidecar (`wa-sidecar/`) — HTTP/IPC, no OpenClaw process |
| **Signal** | (never existed) | Native — `sensor/signal_poller.py` (inbound SSE) + `gateway/channels/signal_cli.py` (outbound) talk to a local `signal-cli daemon --http` over JSON-RPC |

All three seams are now native. OpenClaw is fully removed; no fallback path
exists.

## How a new channel plugs in

The `gateway/egress.py` → `gateway/channels/<name>.py` abstraction is what
keeps each channel self-contained:

1. Add `gateway/channels/<name>.py` implementing `send_text` / `send_photo` /
   `send_document` / `send_reaction` against that channel's native API.
2. Register the channel in `gateway/egress.py`'s channel map.
3. For inbound, add a poller or webhook receiver that normalizes incoming
   messages into aaka's Format-A metadata shape and hands them to the sensor
   router (mirrors `sensor/telegram_poller.py`).
4. Multi-tenant channels (multiple bots/workspaces) key config, tokens, and
   poll offsets by a stable `bot_id` — see `gateway/channels/telegram.py` for
   the pattern (`TELEGRAM_BOT_TOKEN_<ID>` → `tokens/telegram_bots.json`).

Slack (Web API + Socket Mode), the WhatsApp sidecar and Signal (signal-cli
JSON-RPC) all followed this pattern with no shared base class — one small native
module per channel is the right shape here, since each channel's API is different
enough that a unifying abstraction would just add indirection.

Two things Signal made explicit that are worth knowing before adding channel #5:

- **A channel's outbound adapter runs wherever the outbox is flushed.** Replies
  to queued intents are sent by `sensor/flush_outbox.py`, which runs from cron on
  the VPS — not by the Mac executor that produced them. A transport that only
  exists on the Mac can answer zero-token intents and nothing else. Signal makes
  this a setting (`SIGNAL_PLACEMENT`, default `sensor`) and the flusher skips
  rows it cannot deliver instead of erroring forever.
- **Don't name the module after the protocol if the stdlib got there first.**
  `gateway/channels/signal.py` would shadow Python's `signal` module for anything
  run from that directory, so the file is `signal_cli.py` while the channel name
  stays `"signal"`.
