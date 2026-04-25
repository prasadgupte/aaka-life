-- Add expires_at column to outbox_items (idempotent via ALTER TABLE IF NOT EXISTS equivalent)
-- SQLite doesn't support IF NOT EXISTS on ALTER TABLE; run once or guard in Python.
ALTER TABLE outbox_items ADD COLUMN expires_at TEXT;
