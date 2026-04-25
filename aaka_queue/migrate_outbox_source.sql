-- Run once on existing deployments to add source column to outbox_items
-- queue.py _connect() also applies this automatically on first connection.
ALTER TABLE outbox_items ADD COLUMN source TEXT DEFAULT 'telegram';
