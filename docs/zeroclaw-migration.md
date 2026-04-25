# Aaka → ZeroClaw Migration Plan

**New repo:** `/Users/Shared/aaka-repo`
**New config dir:** `/Users/Shared/aaka-repo-config`
**Approach:** Step by step, one goal at a time, verified on local Docker → UTM → VPS
**Docker-first:** Every service runs in Docker from Step 1. No bare-metal installs except the zeroclaw binary on VPS.

---

## Context

Current `aaka/` uses OpenClaw as gateway + LLM routing. ZeroClaw is a Rust-based drop-in with:
- 22+ LLM providers (Gemini via ZeroClaw's LLM routing)
- Native WhatsApp + Telegram support
- `zeroclaw migrate openclaw` tooling
- Much lighter footprint (single binary, <10ms start, 1.5MB RAM)

Key insight: **router_sensor.py is already 100% deterministic** — OpenClaw is a dumb pipe. ZeroClaw is wired the same way: receive message → pass to script → send response. No LLM routing of intents.

---

## Core Architecture Decision: Gateway Abstraction Layer

All claw calls are isolated behind `gateway/adapter.py`. One env var switches backends:

```bash
GATEWAY_BACKEND=zeroclaw   # default
GATEWAY_BACKEND=openclaw   # fallback
```

`gateway/adapter.py` dispatches `send_message()` and `call_llm()` based on this var.

---

## Step Status

| Step | Status | Description |
|------|--------|-------------|
| 1 | ✅ Complete | New repo scaffold, gateway abstraction, Docker-first |
| 2 | 🔧 In progress | ZeroClaw in Docker + Telegram working locally |
| 3 | ⏳ Pending | LLM calls via ZeroClaw + Gemini |
| 4 | ⏳ Pending | Message sending + full confirmation flow |
| 5 | ⏳ Pending | Test on UTM (Apple Silicon VM) |
| 6 | ⏳ Pending | Deploy to remote VPS |

---

## Step 1 — New repo scaffold (complete)

**Goal:** Repo exists with gateway abstraction, CLAUDE.md configured, `docker compose build` succeeds.

**What was done:**
1. Created `/Users/Shared/aaka-repo/` as new git repo
2. Copied `skills/`, `aaka_queue/`, `aaka_config.py`, `executor/` from `aaka/`
3. Created `gateway/` module:
   - `gateway/config.py` — reads `GATEWAY_BACKEND`, `CLAW_BIN`, `GEMINI_API_KEY`
   - `gateway/adapter.py` — `GatewayAdapter`: `send_message()`, `call_llm()`, `gateway_cmd()`
   - `gateway/zeroclaw/agent.toml.example` — zeroclaw config template
   - `gateway/openclaw/agent.yaml.example` — openclaw config template (fallback)
4. Created `llm.py` — calls `gateway/adapter.call_llm()` instead of openclaw directly
5. Created `message_send.py` — calls `gateway/adapter.send_message()` instead of openclaw directly
6. Created `sensor/Dockerfile` — zeroclaw-ready (openclaw fallback active until Step 2)
7. Created `docker-compose.yml` and `docker-compose.prod.yml`
8. Created `CLAUDE.md`, `cli.md`, `docs/setup.md`, `docs/manual.md`
9. Created `/Users/Shared/aaka-repo-config/family/` config dir tree
10. Updated `aaka_config.py` default `AAKA_CONFIG_DIR` → `/Users/Shared/aaka-repo-config`

**Verify:**
```bash
docker compose build
docker compose run --rm sensor python3 -c "from gateway.adapter import GatewayAdapter; print('OK')"
```

---

## Step 2 — ZeroClaw in Docker + Telegram (in progress)

**Goal:** `docker compose up` → Telegram bot receives + responds via ZeroClaw in container.

**Architecture decision:** ZeroClaw runs as a separate Docker service (not embedded in sensor).
Two dispatch patterns are supported:
- **A. Script dispatch:** ZeroClaw pipes message to `python3 sensor/router_sensor.py` (configured in agent.toml)
- **B. HTTP dispatch (fallback):** ZeroClaw calls `POST http://sensor:18789/route` via curl tool

**What was done (Phase 1 — container setup):**
1. Created `zeroclaw/config.toml` — config template with Telegram binding, script dispatch, and HTTP fallback
2. Added `zeroclaw` service to `docker-compose.yml` (dev) using `ghcr.io/zeroclaw-labs/zeroclaw:latest`
3. Added `zeroclaw` service to `docker-compose.prod.yml` (VPS)
4. Switched sensor default `GATEWAY_BACKEND` from `openclaw` to `zeroclaw`
5. Updated prod compose to mount `~/.zeroclaw` instead of `~/.openclaw`
6. Added structured logging to `router_sensor.py` for metadata discovery (Phase 2)

**Next (Phase 2 — discovery):**
1. `docker compose up zeroclaw sensor` — verify both containers start
2. Send `/menu` in Telegram → check `docker compose logs zeroclaw` for dispatch behavior
3. Based on logs, confirm script dispatch works or switch to HTTP dispatch
4. Check `docker compose logs sensor` for metadata format
5. Update metadata parsing if needed

**Verify:**
```bash
docker compose up zeroclaw sensor
# send /menu in Telegram
docker compose logs zeroclaw  # verify message dispatched
docker compose logs sensor    # verify intent matched, check metadata format
```

---

## Step 3 — LLM via ZeroClaw + Gemini (Docker)

**Goal:** `/add physio friday 3pm` extracts correctly using Gemini via ZeroClaw container.

1. Implement `gateway/adapter._call_llm_zeroclaw()` (currently raises NotImplementedError)
2. Update `llm.py` to call via adapter (already done — just needs Step 3 implementation)
3. Test: `/add physio friday 3pm` → structured JSON → pending queue item

**Verify:**
```bash
docker compose run --rm sensor python3 skills/calendar/prepare_event.py "/add physio friday 3pm"
```

---

## Step 4 — Message sending + full confirmation flow (Docker)

**Goal:** Queue worker confirms events back to Telegram via ZeroClaw message send.

1. Implement `gateway/adapter.send_message()` for ZeroClaw backend
2. Test full flow: `/add` → pending → `yes` → executor confirms → Telegram confirmation

**Verify:**
```bash
docker compose run --rm executor python3 executor/queue_worker.py --once
```

---

## Step 5 — Test on UTM (Apple Silicon VM)

**Goal:** Same docker-compose stack runs on UTM, confirming VPS compatibility.

1. Install Docker on UTM Linux VM
2. `docker compose up` — same images as local
3. Repeat Telegram tests
4. Note any ARM64 vs x86_64 issues

---

## Step 6 — Deploy to remote VPS

**Goal:** VPS running zeroclaw + sensor in Docker 24/7, Mac executor polling queue.

1. Update `vps_setup/setup.sh` for Docker + zeroclaw
2. Push Docker images or use compose pull
3. `docker compose -f docker-compose.prod.yml up -d`
4. Verify queue worker on Mac polls VPS queue DB

---

## What Stays Unchanged

| Component | Status |
|-----------|--------|
| `skills/` tree | Fully portable, copied as-is |
| SQLite queue schema | Unchanged |
| `aaka_config.py` | Unchanged (default config dir updated) |
| `executor/queue_worker.py` | Unchanged (uses message_send.py abstraction) |
| `sidecar_sync.py` cron | Unchanged |
| Three-tier routing logic | Unchanged (deterministic, claw-agnostic) |

---

## Switchover / Switchback

```bash
# Switch to ZeroClaw
export GATEWAY_BACKEND=zeroclaw

# Switch back to OpenClaw
export GATEWAY_BACKEND=openclaw
```

`gateway/adapter.py` dispatches based on this env var — no code changes needed.

---

## Previous Migration Plan

The earlier NanoClaw migration plan is in `aaka/docs/nanoclaw-migration.md`.
That plan is superseded by this ZeroClaw migration.
