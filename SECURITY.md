# Security Policy

## Scope

Aaka is a personal assistant that runs on your own machine and VPS. There is no multi-tenant infrastructure or shared backend — all data stays on hardware you control.

Areas in scope for security reports:

- Authentication bypass in the Telegram/WhatsApp/Signal channel gate
- Credential or token leakage via logs, queue DB, or API responses
- Command injection via message parsing
- Insecure defaults that expose data to unintended senders

Out of scope: denial-of-service against a personal instance, issues in third-party dependencies (report those upstream).

## Reporting a vulnerability

Please report privately via GitHub's **Private Vulnerability Reporting**: open the
**Security** tab of this repository → **"Report a vulnerability"**. Your report stays
confidential until a fix is released.

Please include:
1. What the vulnerability is
2. How to reproduce it
3. What impact it could have

Response target: 5 business days for acknowledgement, 30 days for a fix or mitigation.

## What each half can see

Aaka splits into an always-on **Away** half (the VPS sensor) and a **Home** half
(the Mac executor, where OAuth tokens live). A third party — whichever LLM
provider you configure — sees a narrow slice for text extraction only. This
table is the concrete answer to "who can see my data":

| Data class | Away / VPS sensor | Home / Mac executor | Your chosen LLM |
|---|---|---|---|
| Calendar (read) | Yes — read-only mirror (`today.md`, `weekly.md`, `weekly_events.json`, `today_<member>.md`), rsynced by `executor/calendar_sync_and_push.sh` | Yes — full read/write via Google Calendar API | No |
| Calendar (write) / Google OAuth token | No — holds no OAuth token, cannot write the calendar | Yes — the only place calendar writes happen | No |
| Tasks (`tasks.json`) | Yes | Yes | No |
| Lists (shopping etc.) | Yes | Yes | No |
| Notes | Yes | Yes | No |
| Dropped-file staging | Yes — staged on inbound, synced onward | Yes | No |
| Password store | No | Yes | No |
| Mail accounts (POP3/IMAP, Gmail) | No | Yes | No |
| Family vault (filed documents) | No | Yes | No |
| Telegram bot token | Yes — in `.env` | Yes — in `.env`/tokens | No |
| LLM API key | Yes — in `.env` | Yes — in `.env`/tokens | No (the key authenticates *to* the LLM, isn't sent as content) |
| Inbound message content (every message) | Yes — the sensor sees every inbound message to triage intent | Yes | No — only the sentence(s) explicitly sent for extraction (below) |
| Text sent to the LLM | N/A (sent, not stored) | N/A | Yes — only the free-text passed for extraction (`add_event`, `add_task`), the explicit `llm` intent, and whatever a registered agent sends via `/v1/llm` |
| Aaka telemetry/analytics | None exists | None exists | N/A |

Default LLM is Gemini; Anthropic and a local `claude-cli` are also supported.
Whichever you choose, it never reads the calendar, tasks, notes, vault, or any
stored data directly — it only receives the specific text passed in for
extraction (or what an agent explicitly sends to `/v1/llm`), as a one-shot API
call with no memory of prior messages. Zero-token reads (`d`, `w`, `t`, lists,
notes) never touch the LLM.

## Known design decisions

- **Channel gate** — unknown senders get a one-line reply with their own handle (Telegram user ID, WhatsApp number/@lid, Signal number/uuid) and no other data. Group messages from unknown senders are silently dropped. The same gate covers every channel: it keys on `aaka_config.member_by_sender()` plus the configured group ids (`TELEGRAM_GROUP_ID`, `WHATSAPP_GROUP_JID`, `SIGNAL_GROUP_ID`).
- **Signal transport** — `signal-cli`'s JSON-RPC daemon binds `127.0.0.1` only and is never network-reachable; the poller consumes it outbound-only (SSE), so Signal adds no inbound listening port.
- **OAuth credentials** — `config/credentials.json` contains a Desktop app client secret. Desktop app secrets are intentionally distributable (same model as `rclone`, `gcloud auth login`, and similar tools). The token produced by OAuth is yours and lives only on your machine.
- **SQLite queue** — `butler.db` is local-only. The VPS sync copies it over SSH. No external database.
