-- Aaka — SQLite queue schema
-- Applied idempotently: schema.sql is safe to re-run

CREATE TABLE IF NOT EXISTS queue_items (
    id            TEXT PRIMARY KEY,      -- UUID4
    created_at    TEXT NOT NULL,         -- ISO 8601 UTC
    updated_at    TEXT NOT NULL,
    source        TEXT NOT NULL,         -- "whatsapp" | "telegram"
    sender        TEXT NOT NULL,         -- E.164 phone or Telegram chat ID
    channel_id    TEXT NOT NULL,         -- OpenClaw channel for reply routing
    intent        TEXT NOT NULL,         -- "add_event" | "flight_extract" | "add_task"
    raw_message   TEXT NOT NULL,
    payload       TEXT NOT NULL,         -- JSON extraction result
    status        TEXT NOT NULL DEFAULT 'pending',
    -- status lifecycle: pending → awaiting_confirm → confirmed → executing → done/cancelled/error
    confirm_reply TEXT,
    result        TEXT,                  -- JSON on success
    error_msg     TEXT,
    node          TEXT DEFAULT 'sensor',
    retry_count   INTEGER DEFAULT 0,
    content_hash  TEXT,                  -- SHA-256 for dedup
    -- Iter 0: multi-user identity fields (nullable for backward compat)
    user_id       TEXT,
    role          TEXT
);

CREATE INDEX IF NOT EXISTS idx_status     ON queue_items (status);
CREATE INDEX IF NOT EXISTS idx_created_at ON queue_items (created_at);
CREATE INDEX IF NOT EXISTS idx_sender     ON queue_items (sender);


CREATE TABLE IF NOT EXISTS pending_confirms (
    sender     TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL,            -- queue item UUID or synthetic marker (e.g. "list:groceries")
    expires_at TEXT NOT NULL             -- ISO 8601 UTC — auto-expire after 30 min
);


-- Iter 2: VPS-hosted task table (same butler.db, always reachable)
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,       -- UUID4
    created_at  TEXT NOT NULL,          -- ISO 8601 UTC
    updated_at  TEXT NOT NULL,
    namespace   TEXT NOT NULL,          -- member id from aaka.yaml, e.g. "alice"
    title       TEXT NOT NULL,
    notes       TEXT,                   -- human-readable description
    prompt      TEXT,                   -- executable prompt for /run (iter 3)
    skill       TEXT,                   -- skill that created this task, e.g. "book_flight"
    project     TEXT,                   -- entity/project slug e.g. "japan26", "flo172"
    agent       TEXT,                   -- agent id that created this task, e.g. "travel"
    due_date    TEXT,                   -- ISO date YYYY-MM-DD (optional)
    status      TEXT NOT NULL DEFAULT 'todo',  -- todo | done | cancelled
    source      TEXT,                   -- "telegram" | "cli"
    sender      TEXT                    -- sender_id who created it
);

CREATE INDEX IF NOT EXISTS idx_tasks_namespace ON tasks (namespace);
CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks (status);


-- Iter 2.8: Executor → Sensor outbox (feedback path)
CREATE TABLE IF NOT EXISTS outbox_items (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    channel_id          TEXT NOT NULL,
    sender              TEXT NOT NULL,
    text                TEXT NOT NULL,
    reply_to_message_id TEXT,
    status              TEXT DEFAULT 'pending',  -- pending | sent | error | expired
    source              TEXT DEFAULT 'telegram', -- channel source: "telegram" | "whatsapp"
    expires_at          TEXT,                    -- ISO 8601 UTC; NULL = never expires
    silent              INTEGER DEFAULT 0,       -- 1 = disable_notification (automated updates)
    reply_markup        TEXT                     -- JSON: Telegram InlineKeyboardMarkup or null
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_items (status);


-- Agent pub/sub: registered agent identities
CREATE TABLE IF NOT EXISTS agent_registry (
    id                   TEXT PRIMARY KEY,   -- slug: "travel-agent"
    display_name         TEXT NOT NULL,
    key_hash             TEXT NOT NULL,      -- SHA-256(api_key) — never store raw key
    created_at           TEXT NOT NULL,
    last_used_at         TEXT,
    active               INTEGER DEFAULT 1,
    location             TEXT,               -- file path or URL where agent code lives
    key_expires_at       TEXT,               -- ISO 8601 UTC; NULL = never expires
    rate_limit_per_hour  INTEGER DEFAULT 60  -- Iter 9: per-agent API rate limit
);

-- Active subscriptions: agent awaits user reply
CREATE TABLE IF NOT EXISTS agent_reply_requests (
    correlation_id  TEXT PRIMARY KEY,  -- UUID4 chosen by calling agent
    agent_id        TEXT NOT NULL,
    outbox_item_id  TEXT NOT NULL,     -- FK → outbox_items.id (the sent message)
    sender          TEXT NOT NULL,     -- user sender_id to listen for reply from
    channel_id      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,     -- ISO 8601 UTC
    status          TEXT DEFAULT 'waiting'  -- waiting | replied | expired | cancelled
);
CREATE INDEX IF NOT EXISTS idx_arr_sender  ON agent_reply_requests (sender, status);
CREATE INDEX IF NOT EXISTS idx_arr_expires ON agent_reply_requests (expires_at, status);

-- User replies captured for agents
CREATE TABLE IF NOT EXISTS agent_replies (
    id              TEXT PRIMARY KEY,
    correlation_id  TEXT NOT NULL,     -- matches agent_reply_requests.correlation_id
    agent_id        TEXT NOT NULL,
    sender          TEXT NOT NULL,
    text            TEXT NOT NULL,     -- raw user reply text
    received_at     TEXT NOT NULL,
    consumed_at     TEXT               -- NULL = unread; set on DELETE /v1/replies/{id}
);
CREATE INDEX IF NOT EXISTS idx_areply_corr ON agent_replies (correlation_id);


-- Iter 4: Agent job scheduler
CREATE TABLE IF NOT EXISTS agent_jobs (
    id              TEXT PRIMARY KEY,           -- UUID4
    agent_id        TEXT NOT NULL,              -- FK → agent_registry.id
    name            TEXT NOT NULL,              -- human label, e.g. "fetch-travel-emails"
    schedule        TEXT NOT NULL,              -- cron: "*/10 * * * *" or interval: "15m"
    payload         TEXT DEFAULT '{}',          -- JSON passed to executor on fire
    enabled         INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_fired_at   TEXT,                       -- last time job was triggered
    next_fire_at    TEXT,                       -- pre-computed next fire time
    UNIQUE(agent_id, name)
);
CREATE INDEX IF NOT EXISTS idx_jobs_next ON agent_jobs (next_fire_at, enabled);


-- Iter 5: Agent action ledger (tracks resources created by agents for delete-by-hash)
CREATE TABLE IF NOT EXISTS agent_actions (
    id              TEXT PRIMARY KEY,       -- 8-char hex hash (action_id)
    agent_id        TEXT NOT NULL,          -- FK → agent_registry.id
    resource_type   TEXT NOT NULL,          -- "calendar_event" | "task"
    resource_ids    TEXT NOT NULL,          -- JSON array of underlying IDs
    calendar_id     TEXT,                   -- for event deletion (resolved at create time)
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_agent ON agent_actions (agent_id);


-- Sync state: high-water marks for row-level VPS sync (local-only table)
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_updated_at ON queue_items (updated_at);


-- Gmail bulk-sort: per-sender label decisions (deterministic skip + cached label)
CREATE TABLE IF NOT EXISTS gmail_sender_decisions (
    member_id    TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    label        TEXT NOT NULL,
    decided_at   TEXT NOT NULL,
    source       TEXT NOT NULL,        -- 'rule' | 'llm' | 'ask' | 'watch'
    archive      INTEGER DEFAULT 1,    -- 0 = keep in INBOX after labeling
    action       TEXT DEFAULT 'label', -- 'label' | 'trash'
    PRIMARY KEY (member_id, sender_email)
);
CREATE INDEX IF NOT EXISTS idx_gsd_label ON gmail_sender_decisions (member_id, label);

-- Gmail bulk-sort: resumable progress + per-sender message counts (for unsub triage)
CREATE TABLE IF NOT EXISTS gmail_sort_progress (
    member_id       TEXT PRIMARY KEY,
    last_message_id TEXT,
    processed_count INTEGER DEFAULT 0,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS gmail_sender_stats (
    member_id     TEXT NOT NULL,
    sender_email  TEXT NOT NULL,
    msg_count     INTEGER DEFAULT 0,
    last_label    TEXT,
    unsub_url     TEXT,
    last_seen_at  TEXT,
    PRIMARY KEY (member_id, sender_email)
);


-- Iter 8: Error event store (one row per category per day per source file)
CREATE TABLE IF NOT EXISTS error_events (
    id           TEXT PRIMARY KEY,   -- sha256(category+"|"+date_bucket+"|"+source_file)[:32]
    captured_at  TEXT NOT NULL,      -- ISO 8601 UTC — first seen that day
    last_seen_at TEXT NOT NULL,      -- ISO 8601 UTC — most recent occurrence
    category     TEXT NOT NULL,
    count        INTEGER DEFAULT 1,  -- total occurrences on that date bucket
    sample_line  TEXT,               -- representative error line
    source_file  TEXT,
    acknowledged INTEGER DEFAULT 0,  -- 0 = new, 1 = acked via /errors flush
    week_bucket  TEXT NOT NULL       -- YYYY-Www for week-over-week trending
);
CREATE INDEX IF NOT EXISTS idx_error_events_acked ON error_events (acknowledged, captured_at);


-- Iter 9: Scheduled outbound messages (email / telegram / whatsapp)
-- Fired by sensor/scheduled_sender.py cron on VPS; synced Mac→VPS via vps_sync.py.
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    channel       TEXT NOT NULL,                    -- 'email' | 'telegram' | 'whatsapp'
    payload       TEXT NOT NULL,                    -- JSON; shape is channel-specific
    scheduled_at  TEXT NOT NULL,                    -- ISO8601 UTC (always UTC after normalisation)
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | cancelled | error
    result        TEXT,                             -- JSON; e.g. {"message_id": "..."}
    error         TEXT,
    created_at    TEXT NOT NULL,
    sent_at       TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_msg_due   ON scheduled_messages(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_sched_msg_agent ON scheduled_messages(agent_id, status);
