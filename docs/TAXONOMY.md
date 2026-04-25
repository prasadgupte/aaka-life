# Aaka Component Taxonomy

This document is the source of truth for component naming in admin scripts, dashboards, and future work.

---

## 1. Taxonomy (4 kinds)

| Kind | Term | Definition |
|------|------|------------|
| `skill` | **Skill** | A Python callable with one job (calendar, flight, task, mail). Stateless, portable, called by jobs. |
| `service` | **Service** | A managed process unit (launchd plist on Mac, Docker container on VPS). Starts jobs on a schedule or keeps a process alive. |
| `job` | **Job** | A one-shot script invoked by a service or manually via `aaka.sh`. Has a start and end. |
| `auth` | **Auth** | OAuth token lifecycle for a Google account (one per member). |

> **Retired terms:** "sidecar" → service or job. "cron" → job scheduled by a service. "daemon" → service.

---

## 2. Component Inventory

### Services

| ID | Host | Kind | Mechanism | Schedule | Starts |
|----|------|------|-----------|----------|--------|
| `calendar-sync` | Mac | service | launchd `com.aaka.calendarsync.plist` | every 30 min | `sync-calendar` job |
| `queue-worker` | Mac | service | launchd `com.aaka.queueworker.plist` | every 60 sec | `process-queue` job |
| `sensor` | VPS | service | Docker container (`sensor`) | always-on | `route-message` job (long-running) |

### Jobs

| ID | Host | Script | Invoked by | Side-effects |
|----|------|--------|------------|-------------|
| `sync-calendar` | Mac | `skills/calendar/sidecar_sync.py` + rsync | `calendar-sync` service | Writes today.md, weekly.md; rsync to VPS |
| `process-queue` | Mac | `executor/sync_db.sh` → `executor/queue_worker.py` | `queue-worker` service | rsync butler.db, calls skills, writes outbox |
| `route-message` | VPS | `sensor/router_sensor.py` | `sensor` service | Writes butler.db (queue intents), sends inline replies |

### Skills (by domain, from `skills/registry.yaml`)

| Domain | Skill | File | runs_on | Enabled |
|--------|-------|------|---------|---------|
| calendar | `add_event` | `skills/calendar/add_event.py` | executor | yes |
| calendar | `gog` | `skills/calendar/gog.py` | executor | yes |
| calendar | `sidecar_sync` | `skills/calendar/sidecar_sync.py` | executor | yes |
| calendar | `availability` | `skills/calendar/availability.py` | sensor+executor | yes |
| calendar | `prepare_event` | `skills/calendar/prepare_event.py` | sensor | yes |
| calendar | `plan` | `skills/calendar/plan.py` | sensor | yes |
| calendar | `block` | `skills/calendar/block.py` | sensor+executor | yes |
| flights | `extract_flight` | `skills/flights/extract_flight.py` | sensor | yes |
| flights | `update_tracker` | `skills/flights/update_tracker.py` | executor | yes |
| tasks | `task_manager` | `skills/tasks/tasks.py` | executor | yes |
| tasks | `prepare_task` | `skills/tasks/prepare_task.py` | sensor | yes |
| status | `status_core` | `skills/status/status_core.py` | both | yes |
| status | `status_llm` | `skills/status/llm.py` | both | yes |
| mail | `gmail` | `skills/mail/gmail.py` | executor | **disabled** |
| test | `texec`, `tqueue`, `tstatus`, `tthread` | `skills/test/` | both | yes |

### Auth (per Google-enabled member)

| Component | Purpose | File |
|-----------|---------|------|
| `reauth` | Interactive OAuth browser flow | `admin/reauth.py` |
| `auth_check` | Validate + refresh token, heartbeat latency | `skills/auth_check.py` |
| `auth_report` | Security audit (validity, active senders, push to VPS) | `admin/auth.sh` |

---

## 3. Dependency Graph

```
User message
  └─ sensor [service, VPS]
       └─ route-message [job]
            ├─ local intents → respond inline (today.md, weekly.md, health)
            └─ queue intents → prepare_event/prepare_task/extract_flight [skills] → butler.db

butler.db (rsync Mac ↔ VPS every 60s)
  └─ queue-worker [service, Mac]
       └─ process-queue [job]
            └─ skill_loader → add_event/gog/update_tracker/task_manager [skills]
                 └─ auth → Google Calendar API / CSV / Tasks

calendar-sync [service, Mac]
  └─ sync-calendar [job]
       └─ sidecar_sync [skill]
            └─ auth → Google Calendar API
            └─ writes today.md, weekly.md, weekly_events.json
            └─ rsync → VPS /opt/aaka-config/data/calendar/
```

Cross-cutting dependencies (everything depends on these):
- `aaka_config.py` — config loader (reads `$AAKA_CONFIG_DIR/config/aaka.yaml`)
- `gateway/adapter.py` — all messages and LLM calls
- `aaka_queue/queue.py` — shared queue API (sensor + executor)

---

## 5. Open Cleanup Todos (logged 2026-04-20)

### 🔴 Critical
- [ ] `skills/status/` new domain — add to Skills table (Section 2) and Admin table (Section 4)
- [ ] `skills/registry.yaml` — 6 skills missing: `gog`, `availability`, `block`, `prepare_event`, `extract_flight`, `prepare_task`
- [ ] `registry.yaml` `task_manager` points to non-existent `skills/task_manager.py` → fix to `skills/tasks/tasks.py`; remove dead `task_add` entry

### 🟠 High
- [ ] `skills/calendar/plan.py` — add to taxonomy Skills table
- [ ] Registry wrong names: `calendar_sync` → should match `sidecar_sync`; `flight_tracker` → `update_tracker`
- [ ] Deprecated terminology in code comments: `daemon` in `admin/skills.sh:111` and `sensor/entrypoint.sh:58`; `crontab` in `skills/calendar/sidecar_sync.py:4`

### 🟡 Medium
- [ ] 5 admin commands not in taxonomy Section 4: `rebuild`, `exec`, `cal`, `queue`, `status`
- [ ] `sidecar_sync.py` filename uses retired term — rename to `calendar_sync.py` (cascades to plists, shell scripts, imports)
- [ ] Clarify/remove `task_add` dead registry entry (disabled duplicate of `task_manager`)

---

## 4. Admin Script Mapping

| Command | Script | What it touches |
|---------|--------|-----------------|
| `aaka.sh diagnose` | `admin/diagnose.sh` | All components (read-only health check) |
| `aaka.sh deploy` | `admin/deploy.sh` | Services (install plists / Docker), jobs |
| `aaka.sh test` | `admin/test.sh` | Jobs (dry-run smoke tests) |
| `aaka.sh rebuild` | `admin/rebuild.sh` | VPS only: tear down, pull, rebuild, smoke test |
| `aaka.sh exec` | `admin/exec.sh` | Executor command shortcuts |
| `aaka.sh auth` | `admin/auth.sh` | Auth |
| `aaka.sh sync` | `admin/sync.sh` | `sync-calendar` job status |
| `aaka.sh reauth` | `admin/reauth.py` | Auth |
| `aaka.sh skills` | `admin/skills.sh` | Skills (registry inventory) |
| `aaka.sh cal` | `admin/cal.sh` | Calendar access check — list configured calendars |
| `aaka.sh queue` | `admin/queue.sh` | Queue status — item counts, recent items |
| `aaka.sh status` | `skills/status/cmd.py` | Status sub-commands: `code` \| `queue` \| `llm` |
