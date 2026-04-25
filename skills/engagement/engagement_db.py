"""
Aaka — Engagement DB

Local SQLite store for usage events and per-member nudge state.
Privacy-first: stores only member_id (opaque) + intent name + timestamp.
No message content, no payload, no PII beyond what's already in aaka.yaml.

Schema
------
events       — every intent resolved per member
member_state — current level, last_active, nudge history
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config

ENGAGEMENT_DB = aaka_config.DATA_DIR / "engagement" / "engagement.db"

_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   TEXT    NOT NULL,
    intent      TEXT    NOT NULL,
    channel     TEXT    DEFAULT '',
    ts          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_member ON events(member_id, ts);

CREATE TABLE IF NOT EXISTS member_state (
    member_id           TEXT PRIMARY KEY,
    onboarding_level    INTEGER DEFAULT 0,
    last_active         TEXT    DEFAULT '',
    intents_used        TEXT    DEFAULT '[]',   -- JSON array of distinct intent names
    nudge_count         INTEGER DEFAULT 0,
    last_nudge_ts       TEXT    DEFAULT '',
    last_nudge_type     TEXT    DEFAULT '',
    email_fallback_ts   TEXT    DEFAULT '',
    admin_alert_ts      TEXT    DEFAULT ''
);
"""

_RETENTION_DAYS = 90


def _conn() -> sqlite3.Connection:
    ENGAGEMENT_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(ENGAGEMENT_DB))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_DDL)
    return c


# ── Write ─────────────────────────────────────────────────────────────────────

def log_event(member_id: str, intent: str, channel: str = "") -> None:
    ts = datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO events(member_id, intent, channel, ts) VALUES(?,?,?,?)",
            (member_id, intent, channel, ts),
        )
        # Upsert member_state last_active + intents_used
        row = c.execute(
            "SELECT intents_used FROM member_state WHERE member_id=?", (member_id,)
        ).fetchone()
        if row:
            used = json.loads(row["intents_used"] or "[]")
            if intent not in used:
                used.append(intent)
            c.execute(
                "UPDATE member_state SET last_active=?, intents_used=? WHERE member_id=?",
                (ts, json.dumps(used), member_id),
            )
        else:
            c.execute(
                "INSERT INTO member_state(member_id, last_active, intents_used) VALUES(?,?,?)",
                (member_id, ts, json.dumps([intent])),
            )


def record_nudge(member_id: str, nudge_type: str) -> None:
    ts = datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO member_state(member_id, nudge_count, last_nudge_ts, last_nudge_type)
               VALUES(?,1,?,?)
               ON CONFLICT(member_id) DO UPDATE SET
                 nudge_count    = nudge_count + 1,
                 last_nudge_ts  = excluded.last_nudge_ts,
                 last_nudge_type = excluded.last_nudge_type""",
            (member_id, ts, nudge_type),
        )
        if nudge_type == "email_fallback":
            c.execute(
                "UPDATE member_state SET email_fallback_ts=? WHERE member_id=?",
                (ts, member_id),
            )
        if nudge_type == "admin_alert":
            c.execute(
                "UPDATE member_state SET admin_alert_ts=? WHERE member_id=?",
                (ts, member_id),
            )


def set_level(member_id: str, level: int) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO member_state(member_id, onboarding_level)
               VALUES(?,?)
               ON CONFLICT(member_id) DO UPDATE SET onboarding_level=excluded.onboarding_level""",
            (member_id, level),
        )


# ── Read ──────────────────────────────────────────────────────────────────────

def get_state(member_id: str) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM member_state WHERE member_id=?", (member_id,)
        ).fetchone()
    if row:
        d = dict(row)
        d["intents_used"] = json.loads(d.get("intents_used") or "[]")
        return d
    return {
        "member_id": member_id,
        "onboarding_level": 0,
        "last_active": "",
        "intents_used": [],
        "nudge_count": 0,
        "last_nudge_ts": "",
        "last_nudge_type": "",
        "email_fallback_ts": "",
        "admin_alert_ts": "",
    }


def days_since_active(member_id: str) -> float:
    state = get_state(member_id)
    if not state["last_active"]:
        return float("inf")
    try:
        last = datetime.fromisoformat(state["last_active"])
        return (datetime.utcnow() - last).total_seconds() / 86400
    except Exception:
        return float("inf")


def hours_since_nudge(member_id: str) -> float:
    state = get_state(member_id)
    if not state["last_nudge_ts"]:
        return float("inf")
    try:
        last = datetime.fromisoformat(state["last_nudge_ts"])
        return (datetime.utcnow() - last).total_seconds() / 3600
    except Exception:
        return float("inf")


def nudged_today(member_id: str) -> bool:
    """True if any nudge was sent to this member today (local date)."""
    state = get_state(member_id)
    if not state["last_nudge_ts"]:
        return False
    try:
        last = datetime.fromisoformat(state["last_nudge_ts"])
        return last.date() == datetime.utcnow().date()
    except Exception:
        return False


# ── Maintenance ───────────────────────────────────────────────────────────────

def count_uses_any(member_id: str, intents: "list[str]") -> int:
    """Return total event rows for member_id where intent is any of the given list."""
    if not intents:
        return 0
    placeholders = ",".join("?" * len(intents))
    with _conn() as c:
        row = c.execute(
            f"SELECT COUNT(*) FROM events WHERE member_id=? AND intent IN ({placeholders})",
            (member_id, *intents),
        ).fetchone()
    return row[0] if row else 0


def intent_use_counts(member_id: str) -> dict:
    """Return {intent: count} for all intents ever used by member_id."""
    with _conn() as c:
        rows = c.execute(
            "SELECT intent, COUNT(*) as n FROM events WHERE member_id=? GROUP BY intent",
            (member_id,),
        ).fetchall()
    return {r["intent"]: r["n"] for r in rows}


def streak_days(member_id: str) -> int:
    """Return the current active-day streak (consecutive calendar days with ≥1 event).

    Counts backwards from today; a gap of 1 full day with no events breaks the streak.
    Returns 0 if no events today or yesterday.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT date(ts) as day FROM events WHERE member_id=? ORDER BY day DESC",
            (member_id,),
        ).fetchall()
    if not rows:
        return 0
    days = [r["day"] for r in rows]
    today = datetime.utcnow().date()
    streak = 0
    expected = today
    for day_str in days:
        try:
            day = datetime.fromisoformat(day_str).date()
        except Exception:
            break
        if day == expected or day == today:  # allow today or yesterday as start
            if streak == 0 and day < today - timedelta(days=1):
                break  # no activity today or yesterday — streak is 0
            streak += 1
            expected = day - timedelta(days=1)
        else:
            break
    return streak


def purge_old_events() -> int:
    cutoff = (datetime.utcnow() - timedelta(days=_RETENTION_DAYS)).isoformat()
    with _conn() as c:
        cur = c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return cur.rowcount
