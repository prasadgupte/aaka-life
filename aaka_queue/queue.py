"""
Aaka — Shared SQLite queue API.

Both Sensor (writes items) and Executor (reads + updates) import this module.
DB path: $QUEUE_DB or <repo>/queue/butler.db by default.

Connection management: one connection per thread, reused across calls.
Schema and migrations run once per connection, not per call.
"""

import atexit
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Resolve DB path: env var takes precedence (used in Docker / prod rsync)
_DEFAULT_DB = Path(__file__).resolve().parent / "butler.db"
QUEUE_DB = Path(os.environ.get("QUEUE_DB", str(_DEFAULT_DB)))

_SCHEMA = Path(__file__).resolve().parent / "schema.sql"

# Thread-local storage for per-thread singleton connections.
_local = threading.local()
# Track all open connections for cleanup at exit.
_all_conns: list[sqlite3.Connection] = []
_all_conns_lock = threading.Lock()

# Busy timeout: how long a writer waits for a lock before raising (ms).
# 10 s covers queue_worker, agent_api, and sync_db.sh running concurrently.
_BUSY_TIMEOUT_MS = 10_000


def _open_conn() -> sqlite3.Connection:
    """Create a new connection with correct pragmas, schema, and migrations."""
    QUEUE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(QUEUE_DB), timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Schema (all IF NOT EXISTS — safe to run once per connection)
    conn.executescript(_SCHEMA.read_text())
    # Migrations (idempotent, try/except for pre-existing columns)
    for col in ("user_id TEXT", "role TEXT"):
        try:
            conn.execute(f"ALTER TABLE queue_items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE outbox_items ADD COLUMN source TEXT DEFAULT 'telegram'")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    for _col in (
        "ALTER TABLE outbox_items ADD COLUMN silent INTEGER DEFAULT 0",
        "ALTER TABLE outbox_items ADD COLUMN reply_markup TEXT",
    ):
        try:
            conn.execute(_col)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    try:
        fk_info = conn.execute("PRAGMA foreign_key_list(pending_confirms)").fetchall()
        if fk_info:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pending_confirms_new (
                    sender     TEXT PRIMARY KEY,
                    item_id    TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO pending_confirms_new SELECT sender, item_id, expires_at FROM pending_confirms;
                DROP TABLE pending_confirms;
                ALTER TABLE pending_confirms_new RENAME TO pending_confirms;
            """)
            conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    # Iter 6: approval gate + scheduling columns
    for _col in (
        "ALTER TABLE queue_items ADD COLUMN schedule_at TEXT",
        "ALTER TABLE queue_items ADD COLUMN approval_id TEXT",
    ):
        try:
            conn.execute(_col)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    # Iter 7: agent permissions column
    try:
        conn.execute("ALTER TABLE agent_registry ADD COLUMN permissions TEXT DEFAULT '{}'")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # Iter 9: per-agent API rate limit column
    try:
        conn.execute("ALTER TABLE agent_registry ADD COLUMN rate_limit_per_hour INTEGER DEFAULT 60")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # gmail-sort: archive flag + action column on sender decisions
    for _col in (
        "ALTER TABLE gmail_sender_decisions ADD COLUMN archive INTEGER DEFAULT 1",
        "ALTER TABLE gmail_sender_decisions ADD COLUMN action TEXT DEFAULT 'label'",
    ):
        try:
            conn.execute(_col)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    with _all_conns_lock:
        _all_conns.append(conn)
    return conn


def _connect() -> sqlite3.Connection:
    """Return the per-thread singleton connection (creates on first call)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            # Connection was closed externally — recreate
            pass
    conn = _open_conn()
    _local.conn = conn
    return conn


def _close_all() -> None:
    """Close all tracked connections (called at interpreter exit)."""
    with _all_conns_lock:
        for c in _all_conns:
            try:
                c.close()
            except Exception:
                pass
        _all_conns.clear()

atexit.register(_close_all)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(intent: str, raw_message: str, sender: str) -> str:
    blob = f"{intent}|{raw_message.strip()}|{sender}"
    return hashlib.sha256(blob.encode()).hexdigest()


# ── Write ─────────────────────────────────────────────────────────────────────

def write_item(
    intent: str,
    raw_message: str,
    sender: str,
    channel_id: str,
    source: str,
    payload: dict,
) -> str:
    """Insert a new queue item. Returns the item_id (UUID4)."""
    item_id = str(uuid.uuid4())
    now = _now()
    ch = _content_hash(intent, raw_message, sender)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO queue_items
              (id, created_at, updated_at, source, sender, channel_id,
               intent, raw_message, payload, status, content_hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id, now, now, source, sender, channel_id,
                intent, raw_message, json.dumps(payload), "pending", ch,
            ),
        )
    return item_id


# ── Read ──────────────────────────────────────────────────────────────────────

def read_pending() -> list[dict]:
    """Return all items with status='confirmed', ordered by created_at."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_items WHERE status = 'confirmed' ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def read_awaiting_confirm() -> list[dict]:
    """Return items waiting for user confirmation (status='awaiting_confirm')."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_items WHERE status = 'awaiting_confirm' ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_item(item_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM queue_items WHERE id = ?", (item_id,)
        ).fetchone()
    return dict(row) if row else None


def get_item_by_approval_id(approval_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM queue_items WHERE approval_id = ? ORDER BY created_at DESC LIMIT 1",
            (approval_id,)
        ).fetchone()
    return dict(row) if row else None


def read_scheduled_ready() -> list[dict]:
    """Return approved items whose schedule_at has arrived (status='scheduled')."""
    now = _now()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_items WHERE status = 'scheduled' AND schedule_at <= ? ORDER BY schedule_at",
            (now,)
        ).fetchall()
    return [dict(r) for r in rows]


def write_approval_item(
    intent: str,
    raw_message: str,
    sender: str,
    channel_id: str,
    source: str,
    payload: dict,
    approval_id: str,
    schedule_at: str | None = None,
) -> str:
    """Insert a queue item in pending_approval state. Returns item_id."""
    item_id = str(uuid.uuid4())
    now = _now()
    ch = _content_hash(intent, raw_message, sender)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO queue_items
               (id, created_at, updated_at, source, sender, channel_id,
                intent, raw_message, payload, status, content_hash, approval_id, schedule_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, now, now, source, sender, channel_id,
             intent, raw_message, json.dumps(payload), "awaiting_confirm", ch,
             approval_id, schedule_at),
        )
        conn.commit()
    return item_id


# ── Update ────────────────────────────────────────────────────────────────────

def update_status(
    item_id: str,
    status: str,
    result: dict | None = None,
    error_msg: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE queue_items
               SET status = ?, updated_at = ?,
                   result = COALESCE(?, result),
                   error_msg = COALESCE(?, error_msg)
             WHERE id = ?
            """,
            (
                status,
                _now(),
                json.dumps(result) if result is not None else None,
                error_msg,
                item_id,
            ),
        )


def update_payload(item_id: str, payload: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE queue_items SET payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload), _now(), item_id),
        )


def increment_retry(item_id: str) -> int:
    """Increment retry_count and return the new value."""
    with _connect() as conn:
        conn.execute(
            "UPDATE queue_items SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
            (_now(), item_id),
        )
        row = conn.execute(
            "SELECT retry_count FROM queue_items WHERE id = ?", (item_id,)
        ).fetchone()
    return row["retry_count"] if row else 0


# ── Pending-confirm state ─────────────────────────────────────────────────────

def set_pending_confirm(sender: str, item_id: str, expires_minutes: int = 30) -> None:
    expires = (
        datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        # Cancel any existing awaiting_confirm item for this sender before replacing
        old = conn.execute(
            "SELECT item_id FROM pending_confirms WHERE sender = ?", (sender,)
        ).fetchone()
        if old and old["item_id"] != item_id:
            conn.execute(
                "UPDATE queue_items SET status = 'cancelled', updated_at = ? "
                "WHERE id = ? AND status = 'awaiting_confirm'",
                (_now(), old["item_id"]),
            )
        conn.execute(
            "INSERT OR REPLACE INTO pending_confirms (sender, item_id, expires_at) VALUES (?,?,?)",
            (sender, item_id, expires),
        )


def get_pending_confirm(sender: str) -> dict | None:
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_confirms WHERE sender = ? AND expires_at > ?",
            (sender, now),
        ).fetchone()
    return dict(row) if row else None


def clear_pending_confirm(sender: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM pending_confirms WHERE sender = ?", (sender,))


# ── Dedup ─────────────────────────────────────────────────────────────────────

def is_duplicate(intent: str, raw_message: str, sender: str) -> bool:
    """Return True if an active (non-done/cancelled/error) item with the same
    content hash already exists."""
    ch = _content_hash(intent, raw_message, sender)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM queue_items
             WHERE content_hash = ?
               AND status NOT IN ('done','cancelled','error')
            """,
            (ch,),
        ).fetchone()
    return row is not None


# ── Outbox (Executor → Sensor feedback path) ─────────────────────────────────

def write_outbox(
    channel_id: str,
    sender: str,
    text: str,
    reply_to_message_id: str | None = None,
    source: str = "telegram",
    ttl_minutes: int | None = None,
    silent: bool = False,
    reply_markup: "dict | None" = None,
) -> str:
    """Write a pending outbox item. Returns the item_id (UUID4).

    ttl_minutes: if set, the item is marked expired and skipped if not sent
    within that many minutes. Use for scheduled summaries (e.g. ttl_minutes=60).
    Leave None for direct replies (they should always be sent).
    """
    import datetime
    item_id = str(uuid.uuid4())
    expires_at = None
    if ttl_minutes is not None:
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=ttl_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO outbox_items
              (id, created_at, channel_id, sender, text, reply_to_message_id, status, source, expires_at, silent, reply_markup)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (item_id, _now(), channel_id, sender, text, reply_to_message_id, "pending", source, expires_at,
             1 if silent else 0, json.dumps(reply_markup) if reply_markup else None),
        )
    return item_id


def read_pending_outbox() -> list[dict]:
    """Return pending outbox items, expiring stale ones before returning.

    Items with expires_at in the past are marked 'expired' and excluded.
    """
    import datetime
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        # Expire stale items first
        conn.execute(
            "UPDATE outbox_items SET status = 'expired' WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?",
            (now_utc,),
        )
        rows = conn.execute(
            "SELECT * FROM outbox_items WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_outbox_sent(item_id: str, status: str = "sent") -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE outbox_items SET status = ? WHERE id = ?",
            (status, item_id),
        )


# ── Maintenance ───────────────────────────────────────────────────────────────

def expire_confirms() -> int:
    """Delete expired pending_confirms rows. Returns count removed."""
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM pending_confirms WHERE expires_at <= ?", (now,)
        )
    return cur.rowcount


def reset_stuck_executing() -> int:
    """On Executor startup: reset any items stuck in 'executing' back to
    'confirmed' so they are retried. Returns count reset."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE queue_items SET status = 'confirmed', updated_at = ? WHERE status = 'executing'",
            (_now(),),
        )
    return cur.rowcount


# ── Agent pub/sub ──────────────────────────────────────────────────────────────

def register_agent(
    agent_id: str,
    display_name: str,
    api_key: str,
    *,
    location: "str | None" = None,
    key_expires_at: "str | None" = None,
) -> None:
    """Register or update an agent. Stores SHA-256 hash of api_key (never the raw key).

    Args:
        agent_id:       Unique slug, e.g. "travel-agent".
        display_name:   Human-readable name.
        api_key:        Raw API key — only its SHA-256 hash is persisted.
        location:       File path or URL where the agent code lives.
        key_expires_at: ISO 8601 UTC expiry timestamp; None = never expires.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_registry
                (id, display_name, key_hash, created_at, active, location, key_expires_at)
            VALUES (?,?,?,?,1,?,?)
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                key_hash=excluded.key_hash,
                active=1,
                location=excluded.location,
                key_expires_at=excluded.key_expires_at
            """,
            (agent_id, display_name, key_hash, now, location, key_expires_at),
        )


def authenticate_agent(api_key: str) -> "dict | None":
    """Return agent row if key matches an active, non-expired agent, else None.

    Uses hmac.compare_digest for constant-time hash comparison to prevent timing attacks.
    Returns None (and does NOT update last_used_at) if the key has expired.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    now = _now()
    with _connect() as conn:
        # Fetch all active agents and compare in constant time — never short-circuit on hash match
        rows = conn.execute(
            "SELECT * FROM agent_registry WHERE active=1",
        ).fetchall()
        matched = None
        for row in rows:
            if hmac.compare_digest(key_hash, row["key_hash"]):
                matched = row
        if matched:
            # Reject expired keys
            exp = matched["key_expires_at"]
            if exp and exp < now:
                return None
            conn.execute(
                "UPDATE agent_registry SET last_used_at=? WHERE id=?",
                (now, matched["id"]),
            )
    return dict(matched) if matched else None


def create_reply_request(
    correlation_id: str,
    agent_id: str,
    outbox_item_id: str,
    sender: str,
    channel_id: str,
    ttl_minutes: int = 1440,
) -> None:
    """Register that an agent is awaiting a reply from sender for this correlation."""
    now = _now()
    expires = (
        datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_reply_requests
              (correlation_id, agent_id, outbox_item_id, sender, channel_id, created_at, expires_at, status)
            VALUES (?,?,?,?,?,?,?,'waiting')
            """,
            (correlation_id, agent_id, outbox_item_id, sender, channel_id, now, expires),
        )


def get_active_reply_request(sender: str) -> "dict | None":
    """Return the most-recent non-expired waiting reply request for this sender.

    Called by the sensor on every incoming message — O(1) via (sender, status) index.
    """
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM agent_reply_requests
             WHERE sender=? AND status='waiting' AND expires_at > ?
             ORDER BY created_at DESC LIMIT 1
            """,
            (sender, now),
        ).fetchone()
    return dict(row) if row else None


def record_agent_reply(
    correlation_id: str,
    agent_id: str,
    sender: str,
    text: str,
) -> str:
    """Write user's reply into agent_replies; mark request as replied. Returns reply UUID."""
    reply_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_replies (id, correlation_id, agent_id, sender, text, received_at)
            VALUES (?,?,?,?,?,?)
            """,
            (reply_id, correlation_id, agent_id, sender, text, now),
        )
        conn.execute(
            "UPDATE agent_reply_requests SET status='replied' WHERE correlation_id=?",
            (correlation_id,),
        )
    return reply_id


def get_agent_reply(correlation_id: str) -> "dict | None":
    """Return the first unconsumed reply for this correlation_id."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM agent_replies
             WHERE correlation_id=? AND consumed_at IS NULL
             ORDER BY received_at LIMIT 1
            """,
            (correlation_id,),
        ).fetchone()
    return dict(row) if row else None


def consume_agent_reply(correlation_id: str) -> bool:
    """Mark reply as consumed (idempotent). Returns True if a row was updated."""
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE agent_replies SET consumed_at=? WHERE correlation_id=? AND consumed_at IS NULL",
            (now, correlation_id),
        )
    return cur.rowcount > 0


def expire_reply_requests() -> int:
    """Mark stale waiting requests as expired. Call periodically. Returns count expired."""
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE agent_reply_requests SET status='expired' WHERE status='waiting' AND expires_at <= ?",
            (now,),
        )
    return cur.rowcount


# ── Agent job scheduler ───────────────────────────────────────────────────────

def _compute_next_fire(schedule: str, after: "datetime | None" = None) -> str:
    """Compute the next fire time for a schedule string. Returns ISO 8601 UTC.

    Supports:
      - Cron expression: "*/10 * * * *"
      - Interval shorthand: "15m", "1h", "6h", "1d"
    """
    import re as _re
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    base = after or _dt.now(_tz.utc)

    # Interval shorthand: 15m, 1h, 6h, 1d
    m = _re.match(r"^(\d+)([mhd])$", schedule.strip())
    if m:
        val, unit = int(m.group(1)), m.group(2)
        delta = _td(minutes=val) if unit == "m" else _td(hours=val) if unit == "h" else _td(days=val)
        return (base + delta).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Cron expression
    from croniter import croniter
    cron = croniter(schedule, base)
    return cron.get_next(_dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_agent_job(
    agent_id: str, name: str, schedule: str, payload: str = "{}"
) -> dict:
    """Create or update a scheduled job. Returns the job row as dict."""
    now = _now()
    job_id = str(uuid.uuid4())
    next_fire = _compute_next_fire(schedule)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_jobs (id, agent_id, name, schedule, payload, enabled, created_at, updated_at, next_fire_at)
            VALUES (?,?,?,?,?,1,?,?,?)
            ON CONFLICT(agent_id, name) DO UPDATE SET
                schedule=excluded.schedule,
                payload=excluded.payload,
                enabled=1,
                updated_at=excluded.updated_at,
                next_fire_at=excluded.next_fire_at
            """,
            (job_id, agent_id, name, schedule, payload, now, now, next_fire),
        )
        row = conn.execute(
            "SELECT * FROM agent_jobs WHERE agent_id=? AND name=?",
            (agent_id, name),
        ).fetchone()
    return dict(row)


def list_agent_jobs(agent_id: str) -> list:
    """Return all jobs for an agent."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_jobs WHERE agent_id=? ORDER BY name",
            (agent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_agent_job(agent_id: str, job_id: str) -> bool:
    """Delete a job. Returns True if deleted."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM agent_jobs WHERE id=? AND agent_id=?",
            (job_id, agent_id),
        )
    return cur.rowcount > 0


def update_agent_job(agent_id: str, job_id: str, **fields) -> "dict | None":
    """Update specific fields on a job. Allowed: schedule, payload, enabled.
    Recomputes next_fire_at if schedule changes. Returns updated row or None.
    """
    allowed = {"schedule", "payload", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None
    updates["updated_at"] = _now()
    if "schedule" in updates:
        updates["next_fire_at"] = _compute_next_fire(updates["schedule"])
    set_clause = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [job_id, agent_id]
    with _connect() as conn:
        conn.execute(
            f"UPDATE agent_jobs SET {set_clause} WHERE id=? AND agent_id=?",
            vals,
        )
        row = conn.execute(
            "SELECT * FROM agent_jobs WHERE id=? AND agent_id=?",
            (job_id, agent_id),
        ).fetchone()
    return dict(row) if row else None


def get_due_jobs() -> list:
    """Return all enabled jobs whose next_fire_at <= now."""
    now = _now()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_jobs WHERE enabled=1 AND next_fire_at <= ?",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_job_fired(job_id: str) -> None:
    """Update last_fired_at and compute next next_fire_at."""
    now = _now()
    with _connect() as conn:
        row = conn.execute("SELECT schedule FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return
        next_fire = _compute_next_fire(row["schedule"])
        conn.execute(
            "UPDATE agent_jobs SET last_fired_at=?, next_fire_at=?, updated_at=? WHERE id=?",
            (now, next_fire, now, job_id),
        )


# ── Agent action ledger ─────────────────────────────────────────────────────


def record_agent_action(
    agent_id: str, resource_type: str, resource_ids: list[str],
    calendar_id: str = "",
) -> str:
    """Record an agent action in the ledger. Returns the 8-char action_id."""
    action_id = uuid.uuid4().hex[:8]
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO agent_actions (id, agent_id, resource_type, resource_ids, calendar_id, created_at)
               VALUES (?,?,?,?,?,?)""",
            (action_id, agent_id, resource_type, json.dumps(resource_ids), calendar_id or None, now),
        )
    return action_id


def get_agent_action(agent_id: str, action_id: str) -> "dict | None":
    """Look up an action by ID, scoped to agent. Returns dict or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM agent_actions WHERE id=? AND agent_id=?",
            (action_id, agent_id),
        ).fetchone()
    return dict(row) if row else None


def delete_agent_action(agent_id: str, action_id: str) -> bool:
    """Delete the action record from the ledger. Returns True if deleted."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM agent_actions WHERE id=? AND agent_id=?",
            (action_id, agent_id),
        )
    return cur.rowcount > 0


# ── Scheduled outbound messages ─────────────────────────────────────────────


def create_scheduled_message(
    agent_id: str,
    channel: str,
    payload: dict,
    scheduled_at_utc: str,
) -> str:
    """Insert a new scheduled message. Returns its UUID id.

    scheduled_at_utc must be an ISO8601 string in UTC (Z or +00:00).
    payload is a channel-specific dict (will be JSON-serialised).
    """
    mid = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO scheduled_messages
               (id, agent_id, channel, payload, scheduled_at, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (mid, agent_id, channel, json.dumps(payload), scheduled_at_utc, "pending", now, now),
        )
    return mid


def get_scheduled_message(msg_id: str) -> "dict | None":
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM scheduled_messages WHERE id=?", (msg_id,)
        ).fetchone()
    return dict(row) if row else None


def list_scheduled_messages(agent_id: str, status: "str | None" = None) -> list:
    """Return scheduled messages for an agent, newest first."""
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM scheduled_messages WHERE agent_id=? AND status=? "
                "ORDER BY scheduled_at DESC LIMIT 200",
                (agent_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scheduled_messages WHERE agent_id=? "
                "ORDER BY scheduled_at DESC LIMIT 200",
                (agent_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def cancel_scheduled_message(msg_id: str, agent_id: str) -> bool:
    """Mark a pending message as cancelled. Returns True if the row was updated."""
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE scheduled_messages SET status='cancelled', updated_at=? "
            "WHERE id=? AND agent_id=? AND status='pending'",
            (now, msg_id, agent_id),
        )
    return cur.rowcount > 0


def get_due_scheduled_messages(limit: int = 50) -> list:
    """Return pending messages whose scheduled_at <= now. Used by the VPS sender cron."""
    now = _now()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_messages "
            "WHERE status='pending' AND scheduled_at <= ? "
            "ORDER BY scheduled_at LIMIT ?",
            (now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_scheduled_sent(msg_id: str, result: dict) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE scheduled_messages SET status='sent', result=?, sent_at=?, updated_at=? "
            "WHERE id=?",
            (json.dumps(result), now, now, msg_id),
        )


def mark_scheduled_error(msg_id: str, error: str) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE scheduled_messages SET status='error', error=?, updated_at=? WHERE id=?",
            (error, now, msg_id),
        )
