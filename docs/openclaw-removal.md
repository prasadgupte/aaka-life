# OpenClaw Removal + Multi-Channel Runbook

Status: **planned** (start via remote session). Owner: PG + Claude.
Companion to `docs/zeroclaw-migration.md` (historical gateway notes).

The goal: make the **default** install OpenClaw-free — Telegram-first, LLM via a
direct API — then add WhatsApp (own Baileys sidecar), multi-bot Telegram, and Slack
as clean additive channels. Ship the launch on Telegram (single + multi-bot) and Slack.

---

## 1. What OpenClaw actually does today (the coupling)

Traced against the repo. OpenClaw survives in exactly **three seams**:

| Seam | Where | Native alternative status |
|------|-------|---------------------------|
| **Telegram** | — | **Already native.** `sensor/telegram_poller.py` (inbound long-poll) + `gateway/channels/telegram.py` (outbound) hit `api.telegram.org` directly. `entrypoint.sh` *explicitly disables* OpenClaw's TG connector. **OpenClaw not involved.** |
| **LLM calls** | `llm.py` → `gateway/adapter.py` `_call_llm_openclaw` (`openclaw agent 'llm'`) | Adapter **already has a direct-Gemini branch** (`GEMINI_API_KEY`). Just needs to become the default + an Anthropic branch. |
| **WhatsApp** | `gateway/channels/whatsapp.py` → `gateway/backends/openclaw.py` (subprocess CLI, ~83 lines) wrapping **Baileys**; inbound via the OpenClaw daemon | The **only** hard dependency. Replace with own Baileys sidecar, or drop WhatsApp for Telegram-first. |

Everything else that greps for `openclaw` is references in admin scripts, Docker, and
docs. The gateway abstraction (`gateway/egress.py` → `channels/<name>.py`) is the reason
this is contained, not a rewrite.

**Blast radius (26 files):** `gateway/{adapter,config,egress,ingress,agent_api,__init__}.py`,
`gateway/channels/{whatsapp,__init__}.py`, `gateway/backends/{openclaw,base}.py`, `llm.py`,
`message_send.py`, `sensor/{router_sensor,telegram_poller,scheduled_summaries,Dockerfile,entrypoint.sh}`,
`docker-compose*.yml`, `requirements_sensor.txt`, `admin/{aaka,setup,diagnose,test,backup,setup_check}.*`.

---

## 2. Phased removal — "Telegram-first, OpenClaw-out" (~2–3 days)

### Phase 1 — LLM off OpenClaw · ~0.5d
- `gateway/adapter.py`: promote the existing gemini-direct branch to `_call_llm_direct`;
  add an Anthropic branch (`ANTHROPIC_API_KEY`, Messages API) for Claude.
- `gateway/config.py`: default `BACKEND` → `direct` (was `openclaw`); add `LLM_PROVIDER=gemini|anthropic`.
- `llm.py`: dispatch to the direct path.
- **Validate:** re-run the intent-extraction prompts (`add_event`, `add_task`) against the
  direct model; confirm parse parity. (These are the only LLM-dependent skills.)

### Phase 2 — Telegram outbound fully native · ~0.5d
- `message_send.py` / `adapter.send_message`: route `telegram` through `gateway.egress.send`
  (native path `send_message_direct_tg` already exists) instead of spawning the OpenClaw CLI.
- `sensor/router_sensor.py`: already uses egress for photos/reactions — extend to text.

### Phase 3 — WhatsApp made optional · ~0.5d
- `gateway/config.py` + `egress.py` + `channels/__init__.py`: a `CHANNELS=telegram` (comma list)
  flag. When WhatsApp is off, never import `channels/whatsapp.py` or start any daemon.

### Phase 4 — De-OpenClaw the container · ~0.5d
- `sensor/Dockerfile`: drop `npm install -g openclaw @whiskeysockets/baileys`; drop **Node entirely**
  when WhatsApp is off (the poller is pure Python). Removes Playwright/pdfjs bloat + the 400 MB heap cap.
- `sensor/entrypoint.sh`: remove OpenClaw daemon start + `agent.yaml` templating; run `telegram_poller.py`
  + queue worker only.
- `docker-compose*.yml`: drop `.openclaw` volumes/env; change default `GATEWAY_BACKEND`.

### Phase 5 — Cleanup + runbook · ~0.5d
- `admin/{aaka,setup,diagnose,test}.sh`, `admin/setup_check.py`: strip OpenClaw checks; per the
  standing rule, **add native-path checks to `diagnose.sh`/`test.sh`**.
- `requirements_sensor.txt` note, `README.md`, `CLAUDE.md` gateway section.

**Sequencing:** Phases 1→2 give a running Telegram-first stack that works with OpenClaw
*uninstalled* even before touching Docker — validate live, then 3→5 delete it.

---

## 3. Multi-bot Telegram (launch feature)

Today both sides assume **one** bot: `telegram_poller.py` reads `TELEGRAM_BOT_TOKEN` (env),
`channels/telegram.py` resolves a single global token (env or `tokens/message_send.json`).
Multi-bot = per-bot config + token selection on **both** inbound and outbound.

**Design:**
1. **Config** — `aaka.yaml`: `telegram.bots: [{ id, token_ref, scope }]` (token in `tokens/`, not yaml).
   `id` is a stable slug (e.g. `family`, `demo`).
2. **Inbound** — parameterize the poller by `bot_id` + token; run **one poller per bot**
   (a supervisor that spawns N async long-poll loops in one process, or N launchd/compose services).
   Tag the Format-A metadata the poller emits with `bot_id`.
3. **Queue** — carry `bot_id` on queue items + agent reply-request rows, so the executor's reply
   goes back out via the **same** bot.
4. **Outbound** — `channels/telegram.py`: resolve token by `OutboundMessage.bot_id` (drop the module-global).
5. **Isolation** — offsets are already per-bot-safe if keyed by `bot_id` (`_load_offset`/`_save_offset`
   currently single-file — key them by bot).

**Effort:** ~1.5–2d. Pairs naturally with Phase 2 (both touch the channel token path) — do it right after.

**Use cases it unlocks:** a public **demo bot** (nightly-wiped) alongside the family bot on one
executor; per-household bots for a small multi-tenant; separate work/home bots.

---

## 4. Slack (launch channel) · ~1–2d

New native channel, no OpenClaw:
- `gateway/channels/slack.py` — `send_text/photo/document/reaction` via Slack **Web API** (`chat.postMessage`, `files.upload`).
- Inbound — **Socket Mode** listener (WebSocket, no public webhook needed → stays local-first) → build
  Format-A metadata → router. Run as its own poller service, mirrors `telegram_poller.py`.
- Config — `slack.workspaces: [{ id, bot_token_ref, app_token_ref }]`; multi-workspace = same pattern as multi-bot.
- Register `slack` in `egress.py` channel map.

Discord later is near-free off the same pattern. **No unifying library** is worth it — WhatsApp is the
outlier (reverse-engineered WhatsApp Web via Baileys); Telegram/Slack/Discord all have clean official bot APIs,
so one small native module per channel *is* the right architecture.

---

## 5. Optional — WhatsApp without OpenClaw · ~2–4d

Own Baileys sidecar (lift patterns from `/Users/Shared/tools/wa-backup`):
- `wa-sidecar/` Node service: QR session, inbound → SQLite queue, outbound HTTP/IPC endpoint.
- `channels/whatsapp.py`: swap the OpenClaw subprocess for HTTP/IPC to the sidecar.
- `backends/openclaw.py`: delete (or keep behind a legacy flag).
- **Migration note:** users **re-scan QR once** (OpenClaw's WA session doesn't transfer). Telegram bot
  tokens are portable, so Telegram users see no disruption.

---

## 6. Test plan — verifying channels reliably

**What exists:** `admin/test.sh` (170 checks — skills, routing, aliases, buy/notes/tasks/drop,
image compression) + runtime smoke tests `skills/test/{tqueue,texec,tstatus,tthread}.py`
(`/tqueue`, `/texec`, `/tthread`, `/tstatus`). Strong on **skill logic**; **thin on channel adapters**.

**What to add (before/with the channel work):**

1. **Adapter unit tests (mocked API, no network)** — per channel (telegram, slack, whatsapp-sidecar):
   assert correct API method + payload + **token selection** + Telegram 4096-char chunking + Markdown
   fallback. Mock `urllib`/SDK.
2. **Inbound parse tests (fixtures)** — feed a captured `getUpdates` / Slack event JSON → assert the
   Format-A metadata (`sender_id`, `channel_id`, `bot_id`, text, media) is built correctly.
3. **Multi-bot routing test** — two configured bots; message on bot A ⇒ reply token = A; bot B ⇒ B.
   Pure logic, fully mockable. This is the one that de-risks the launch.
4. **Round-trip integration (recorded)** — extend `tthread` to assert sensor→queue→executor→reply lands
   with the right `bot_id`, channel send mocked/cassette.
5. **Live check (opt-in, gated)** — `TEST_LIVE=1` mode in `test.sh` that actually sends to a throwaway
   Telegram chat / Slack channel and reads it back. Never in default CI.

Land all of the above in `admin/test.sh` (standing rule) + a new `skills/test/tchannel.py` smoke.
**Definition of "reliable enough to launch":** 1–4 green in CI for every channel we ship, plus a manual
`TEST_LIVE` pass on real Telegram (single + 2 bots) and one Slack workspace.

---

## 7. "How far we go" — recommended launch scope

Order for the remote session(s), each independently shippable:

1. **Phase 1 + 2** — OpenClaw-free Telegram + direct LLM. *(Foundation; validate live.)*
2. **Test plan items 1–4** — channel adapter + multi-bot routing tests. *(De-risk.)*
3. **Multi-bot Telegram** (§3). *(Launch feature: family bot + demo bot on one box.)*
4. **Phase 3–5** — actually delete OpenClaw from Docker/admin/docs. *(Ship the lean image.)*
5. **Slack** (§4). *(Second channel.)*
6. *(Later)* WhatsApp Baileys sidecar (§5).

Realistic first-session target: **1 + 2 partial** (a running OpenClaw-free Telegram stack with adapter
tests). A focused follow-up gets multi-bot + delete + Slack.

---

## 8. Risks / notes
- LLM parity: direct API vs `openclaw agent` — validate intent-extraction prompts (low risk, only 2 skills).
- WhatsApp session doesn't migrate (re-scan QR); Telegram tokens are portable.
- Multi-bot: the sharp edge is **reply routing** — every queue/reply row must carry `bot_id`. Test #3 covers it.
- Keep the `gateway/egress.py` + `channels/*` abstraction — it's what keeps all of this contained.
- Slack Socket Mode keeps us local-first (no public webhook). Don't fall back to the Events API webhook.
