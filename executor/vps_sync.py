"""
executor/vps_sync.py — Row-level VPS sync for queue_worker daemon.

Replaces sync_db.sh: no more rsync of butler.db.  Instead:
  - Pull new queue_items from VPS via SSH + sqlite3 -json
  - Push status updates and outbox rows via SSH + piped Python
  - rsync for non-DB files (staging, lists, notes, birthday)

Called periodically from queue_worker.py's daemon loop.
"""

import dataclasses
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


@dataclasses.dataclass
class SyncConfig:
    vps_host: str
    vps_db_path: str
    config_dir: str
    vps_config_root: str
    ssh_timeout: int = 10
    rsync_timeout: int = 15


@dataclasses.dataclass
class SyncStats:
    pulled: int = 0
    pushed: int = 0
    outbox: int = 0
    reply_reqs: int = 0
    replies_pulled: int = 0
    files_ok: bool = True
    duration_ms: int = 0


def load_config() -> "SyncConfig | None":
    """Build SyncConfig from env vars. Returns None if VPS sync not configured."""
    vps_host = os.environ.get("VPS_HOST", "")
    vps_db = os.environ.get("VPS_DB_PATH", "")
    if not vps_host or not vps_db:
        return None
    config_dir = os.environ.get("AAKA_CONFIG_DIR", "/Users/Shared/aaka-repo-config")
    # Derive VPS config root: /opt/aaka-config/data/queue/butler.db → /opt/aaka-config
    vps_root = str(Path(vps_db).parent.parent.parent)
    return SyncConfig(
        vps_host=vps_host,
        vps_db_path=vps_db,
        config_dir=config_dir,
        vps_config_root=vps_root,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ssh_run(cfg: SyncConfig, cmd: str, *, timeout: int = 30,
             stdin_data: str = "") -> subprocess.CompletedProcess:
    """Run a command on VPS via SSH."""
    args = [
        "ssh",
        "-o", f"ConnectTimeout={cfg.ssh_timeout}",
        "-o", "BatchMode=yes",
        cfg.vps_host,
        cmd,
    ]
    return subprocess.run(
        args, input=stdin_data or None,
        capture_output=True, text=True, timeout=timeout,
    )


def _rsync(cfg: SyncConfig, src: str, dst: str, *, delete: bool = False) -> bool:
    """Run rsync. Returns True on success."""
    cmd = ["rsync", "-az", f"--timeout={cfg.rsync_timeout}"]
    if delete:
        cmd.append("--delete")
    cmd.extend([src, dst])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# ── Sync state (local-only high-water marks) ─────────────────────────────────


def _get_hwm(key: str) -> str:
    """Read a high-water mark from sync_state table."""
    from aaka_queue.queue import _connect
    conn = _connect()
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else "1970-01-01T00:00:00Z"


def _set_hwm(key: str, value: str) -> None:
    """Write a high-water mark to sync_state table."""
    from aaka_queue.queue import _connect
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


# ── Pull: VPS → Mac ─────────────────────────────────────────────────────────


def _pull_queue_rows(cfg: SyncConfig) -> int:
    """Pull new queue_items from VPS via SSH + sqlite3 -json. Returns count."""
    from aaka_queue.queue import _connect

    hwm = _get_hwm("pull_hwm")
    sql = (
        f"SELECT * FROM queue_items WHERE updated_at > '{hwm}' "
        f"ORDER BY updated_at LIMIT 500"
    )
    r = _ssh_run(cfg, f"sqlite3 -json '{cfg.vps_db_path}' \"{sql}\"")
    if r.returncode != 0 or not r.stdout.strip():
        return 0

    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return 0
    if not rows:
        return 0

    conn = _connect()
    inserted = 0
    max_updated = hwm
    for row in rows:
        rid = row.get("id", "")
        if not rid:
            continue
        # Track max updated_at for high-water mark
        row_updated = row.get("updated_at", "")
        if row_updated > max_updated:
            max_updated = row_updated

        existing = conn.execute(
            "SELECT status, updated_at FROM queue_items WHERE id = ?", (rid,)
        ).fetchone()

        if existing is None:
            # New row — insert it
            cols = list(row.keys())
            placeholders = ",".join("?" * len(cols))
            conn.execute(
                f"INSERT INTO queue_items ({','.join(cols)}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
            inserted += 1
        else:
            # Row exists locally. If VPS cancelled it and we haven't processed it,
            # update local status to match.
            vps_status = row.get("status", "")
            local_status = existing["status"]
            if vps_status == "cancelled" and local_status in ("pending", "confirmed", "awaiting_confirm"):
                conn.execute(
                    "UPDATE queue_items SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (row_updated, rid),
                )

    conn.commit()
    if max_updated > hwm:
        _set_hwm("pull_hwm", max_updated)
    return inserted


def _pull_files(cfg: SyncConfig) -> bool:
    """rsync pull staging/, lists/, notes/ from VPS. Returns True if all OK."""
    ok = True
    for subdir in ("staging", "lists", "notes"):
        local = f"{cfg.config_dir}/data/{subdir}/"
        os.makedirs(local, exist_ok=True)
        remote = f"{cfg.vps_host}:{cfg.vps_config_root}/data/{subdir}/"
        if not _rsync(cfg, remote, local):
            ok = False
    return ok


def _sync_tasks(cfg: SyncConfig) -> bool:
    """Bidirectional merge of tasks.json between Mac and VPS.

    Both sides can create tasks (Mac via agent gateway, VPS via /addtask).
    Merge by task ID — union of both, preferring the copy with a later
    updated_at/done_at/created_at for conflicts.
    """
    local_dir = Path(cfg.config_dir) / "data" / "tasks"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_file = local_dir / "tasks.json"

    # Read local tasks
    local_tasks: list[dict] = []
    if local_file.exists():
        try:
            local_tasks = json.loads(local_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Read VPS tasks via SSH
    vps_tasks_path = f"{cfg.vps_config_root}/data/tasks/tasks.json"
    r = _ssh_run(cfg, f"cat '{vps_tasks_path}' 2>/dev/null || echo '[]'")
    vps_tasks: list[dict] = []
    if r.returncode == 0 and r.stdout.strip():
        try:
            vps_tasks = json.loads(r.stdout)
        except json.JSONDecodeError:
            pass

    if not local_tasks and not vps_tasks:
        return True

    # Merge: index by ID, prefer the one with later timestamp.
    # updated_at is the primary key (set on every mutation since 2026-05).
    # Fallback chain handles tasks that predate updated_at.
    def _ts(t: dict) -> str:
        return t.get("updated_at") or t.get("done_at") or t.get("snooze_until") or t.get("created_at") or ""

    by_id: dict[str, dict] = {}
    for t in local_tasks:
        tid = t.get("id", "")
        if tid:
            by_id[tid] = t
    for t in vps_tasks:
        tid = t.get("id", "")
        if not tid:
            continue
        if tid not in by_id or _ts(t) > _ts(by_id[tid]):
            by_id[tid] = t

    merged = list(by_id.values())

    # Write to both sides
    merged_json = json.dumps(merged, ensure_ascii=False, indent=2)
    try:
        local_file.write_text(merged_json)
    except OSError:
        return False

    # Push merged file to VPS via stdin (avoids command-length limits on large files)
    write_cmd = (
        f"python3 -c \""
        f"import sys, pathlib; "
        f"p = pathlib.Path('{vps_tasks_path}'); "
        f"p.parent.mkdir(parents=True, exist_ok=True); "
        f"p.write_text(sys.stdin.read())"
        f"\""
    )
    r2 = _ssh_run(cfg, write_cmd, stdin_data=merged_json, timeout=15)

    # Archive old done/deleted tasks on Mac (after successful sync to VPS).
    if r2.returncode == 0:
        try:
            _base = os.environ.get("AAKA_BASE", str(Path(__file__).resolve().parent.parent))
            if _base not in sys.path:
                sys.path.insert(0, _base)
            from skills.tasks.local_tasks import archive_done_tasks
            n = archive_done_tasks()
            if n:
                print(f"[vps_sync] archived {n} old task(s)")
        except Exception as e:
            print(f"[vps_sync] archive_done_tasks skipped: {e}")

    return r2.returncode == 0


# ── Push: Mac → VPS ─────────────────────────────────────────────────────────


# Template for the Python script sent to VPS via SSH.
# Data is embedded as base64 to avoid all quoting issues.
# Placeholders: {db_path}, {table}, {mode}, {data_b64}
_VPS_WRITE_SCRIPT = """\
import json, sqlite3, base64
rows = json.loads(base64.b64decode("{data_b64}"))
if not rows:
    print(0)
    raise SystemExit
conn = sqlite3.connect("{db_path}")
conn.execute("PRAGMA busy_timeout=10000")
conn.execute("PRAGMA journal_mode=WAL")
cols = list(rows[0].keys())
ph = ",".join("?" * len(cols))
cs = ",".join(cols)
pk = "{pk}"
n = 0
for r in rows:
    v = [r.get(c) for c in cols]
    try:
        if "{mode}" == "upsert":
            sets = ",".join(f"{{c}}=excluded.{{c}}" for c in cols if c != pk)
            conn.execute(f"INSERT INTO {table} ({{cs}}) VALUES ({{ph}}) ON CONFLICT({{pk}}) DO UPDATE SET {{sets}}", v)
        else:
            conn.execute(f"INSERT OR IGNORE INTO {table} ({{cs}}) VALUES ({{ph}})", v)
        n += 1
    except Exception as e:
        pass
conn.commit()
print(n)
"""


def _push_to_vps(cfg: SyncConfig, table: str, rows: list[dict],
                 mode: str = "insert", pk: str = "id") -> int:
    """Push rows to a VPS table via SSH + piped Python. Returns count.

    Pipes a self-contained Python script (with embedded base64 data) through
    SSH stdin → ``python3 -``.  No shell quoting headaches.
    """
    if not rows:
        return 0
    import base64
    data_b64 = base64.b64encode(json.dumps(rows).encode()).decode()
    script = _VPS_WRITE_SCRIPT.format(
        db_path=cfg.vps_db_path, table=table, mode=mode, data_b64=data_b64, pk=pk,
    )
    r = _ssh_run(cfg, "python3 -", stdin_data=script, timeout=60)
    if r.returncode != 0:
        print(f"[sync] push {table} error: {r.stderr.strip()}")
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def _push_heartbeats(cfg: SyncConfig) -> int:
    """Push today's signal heartbeats Mac→VPS so the sensor watchdog knows which
    scheduled Mac jobs ran. INSERT OR IGNORE — existence is all the watchdog checks."""
    from aaka_queue.queue import _connect
    from datetime import date
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM signal_heartbeats WHERE ok_date >= ?",
            (date.today().isoformat(),),
        ).fetchall()
    except Exception:
        return 0  # table not created yet
    if not rows:
        return 0
    # Ensure the VPS table exists first, so day-1 pushes land (avoids a false miss).
    ddl = ("CREATE TABLE IF NOT EXISTS signal_heartbeats "
           "(name TEXT NOT NULL, ok_date TEXT NOT NULL, ts TEXT NOT NULL, "
           "PRIMARY KEY(name, ok_date));")
    _ssh_run(cfg, f"sqlite3 '{cfg.vps_db_path}' \"{ddl}\"", timeout=15)
    return _push_to_vps(cfg, "signal_heartbeats", [dict(r) for r in rows], mode="insert")


def _push_status_updates(cfg: SyncConfig) -> int:
    """Push locally-processed queue items (done/error) back to VPS."""
    from aaka_queue.queue import _connect

    hwm = _get_hwm("push_hwm")
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM queue_items "
        "WHERE status IN ('done', 'error', 'cancelled') AND updated_at > ? "
        "ORDER BY updated_at LIMIT 200",
        (hwm,),
    ).fetchall()
    if not rows:
        return 0

    dicts = [dict(r) for r in rows]
    pushed = _push_to_vps(cfg, "queue_items", dicts, mode="upsert")

    if pushed:
        max_updated = max(d["updated_at"] for d in dicts)
        _set_hwm("push_hwm", max_updated)
    return pushed


def _push_outbox(cfg: SyncConfig) -> int:
    """Push new outbox_items to VPS for flush_outbox.py to send.

    For outbox items that carry an inline keyboard (reply_markup set), also push
    the matching pending_confirms row for that sender — atomically with the message.
    This ensures the VPS sensor can resolve the user's "yes"/"cancel" tap without
    a separate polling cycle.  pending_confirms rows whose sender is no longer in
    Mac's table are also cleaned up on VPS (post-confirm/cancel garbage collection).
    """
    from aaka_queue.queue import _connect

    hwm = _get_hwm("outbox_hwm")
    conn = _connect()
    # Exclude source='web' — those are handled locally by webui SSE and
    # must never be synced to the VPS sensor for flush_outbox dispatch.
    rows = conn.execute(
        "SELECT * FROM outbox_items "
        "WHERE created_at > ? AND COALESCE(source,'telegram') != 'web' "
        "ORDER BY created_at LIMIT 200",
        (hwm,),
    ).fetchall()
    if not rows:
        return 0

    dicts = [dict(r) for r in rows]
    pushed = _push_to_vps(cfg, "outbox_items", dicts, mode="insert")

    if pushed:
        max_created = max(d["created_at"] for d in dicts)
        _set_hwm("outbox_hwm", max_created)

    # For any outbox item with an inline keyboard, push its pending_confirm so the
    # VPS sensor can handle the reply immediately — no separate polling step needed.
    keyboard_senders = {d["sender"] for d in dicts if d.get("reply_markup")}
    if keyboard_senders:
        pc_rows = conn.execute(
            f"SELECT sender, item_id, expires_at FROM pending_confirms "
            f"WHERE sender IN ({','.join('?' * len(keyboard_senders))})",
            list(keyboard_senders),
        ).fetchall()
        if pc_rows:
            _push_to_vps(cfg, "pending_confirms", [dict(r) for r in pc_rows],
                         mode="upsert", pk="sender")

    # Clean up VPS pending_confirms that no longer exist on Mac (approved/cancelled).
    mac_senders_result = conn.execute("SELECT sender FROM pending_confirms").fetchall()
    mac_senders = {r["sender"] for r in mac_senders_result}
    r = _ssh_run(cfg, f"sqlite3 -json '{cfg.vps_db_path}' \"SELECT sender FROM pending_confirms\"")
    if r.returncode == 0 and r.stdout.strip():
        try:
            vps_senders = {row["sender"] for row in json.loads(r.stdout)}
            stale = vps_senders - mac_senders
            for sender in stale:
                safe = sender.replace("'", "''")
                _ssh_run(cfg, f"sqlite3 '{cfg.vps_db_path}' \"DELETE FROM pending_confirms WHERE sender='{safe}'\"")
        except (json.JSONDecodeError, KeyError):
            pass

    return pushed


def _push_reply_requests(cfg: SyncConfig) -> int:
    """Push agent_reply_requests to VPS so sensor can intercept user replies.

    Must run BEFORE _push_outbox so the request exists in VPS DB before
    flush_outbox.py sends the Telegram message with inline keyboard buttons.
    """
    from aaka_queue.queue import _connect

    hwm = _get_hwm("reply_req_hwm")
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM agent_reply_requests WHERE created_at > ? ORDER BY created_at LIMIT 200",
        (hwm,),
    ).fetchall()
    if not rows:
        return 0

    dicts = [dict(r) for r in rows]
    pushed = _push_to_vps(
        cfg, "agent_reply_requests", dicts,
        mode="upsert", pk="correlation_id",
    )

    if pushed:
        max_created = max(d["created_at"] for d in dicts)
        _set_hwm("reply_req_hwm", max_created)
    return pushed


def _pull_agent_replies(cfg: SyncConfig) -> int:
    """Pull agent_replies from VPS to Mac so gateway can serve them to polling agents."""
    from aaka_queue.queue import _connect

    hwm = _get_hwm("reply_pull_hwm")
    sql = (
        f"SELECT * FROM agent_replies WHERE received_at > '{hwm}' "
        f"ORDER BY received_at LIMIT 200"
    )
    r = _ssh_run(cfg, f"sqlite3 -json '{cfg.vps_db_path}' \"{sql}\"")
    if r.returncode != 0 or not r.stdout.strip():
        return 0

    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return 0
    if not rows:
        return 0

    conn = _connect()
    inserted = 0
    max_received = hwm
    for row in rows:
        rid = row.get("id", "")
        if not rid:
            continue
        row_ts = row.get("received_at", "")
        if row_ts > max_received:
            max_received = row_ts
        cols = list(row.keys())
        placeholders = ",".join("?" * len(cols))
        try:
            conn.execute(
                f"INSERT OR IGNORE INTO agent_replies ({','.join(cols)}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
            inserted += 1
        except Exception:
            pass
        # Mark the corresponding request as replied on Mac side
        corr_id = row.get("correlation_id", "")
        if corr_id:
            conn.execute(
                "UPDATE agent_reply_requests SET status='replied' "
                "WHERE correlation_id=? AND status='waiting'",
                (corr_id,),
            )

    conn.commit()
    if max_received > hwm:
        _set_hwm("reply_pull_hwm", max_received)
    return inserted


def _push_scheduled_messages(cfg: SyncConfig) -> int:
    """Push new/cancelled scheduled_messages to VPS so the sensor cron can fire them."""
    from aaka_queue.queue import _connect

    hwm = _get_hwm("sched_msg_push_hwm")
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM scheduled_messages "
        "WHERE updated_at > ? AND status IN ('pending', 'cancelled') "
        "ORDER BY updated_at LIMIT 200",
        (hwm,),
    ).fetchall()
    if not rows:
        return 0

    dicts = [dict(r) for r in rows]
    pushed = _push_to_vps(cfg, "scheduled_messages", dicts, mode="upsert")

    if pushed:
        max_updated = max(d["updated_at"] for d in dicts)
        _set_hwm("sched_msg_push_hwm", max_updated)
    return pushed


def _pull_scheduled_message_results(cfg: SyncConfig) -> int:
    """Pull sent/error status + result back from VPS to Mac."""
    from aaka_queue.queue import _connect

    hwm = _get_hwm("sched_msg_pull_hwm")
    sql = (
        f"SELECT * FROM scheduled_messages "
        f"WHERE updated_at > '{hwm}' AND status IN ('sent','error') "
        f"ORDER BY updated_at LIMIT 200"
    )
    r = _ssh_run(cfg, f"sqlite3 -json '{cfg.vps_db_path}' \"{sql}\"")
    if r.returncode != 0 or not r.stdout.strip():
        return 0

    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return 0
    if not rows:
        return 0

    conn = _connect()
    updated = 0
    max_ts = hwm
    for row in rows:
        if row.get("updated_at", "") > max_ts:
            max_ts = row["updated_at"]
        try:
            conn.execute(
                """UPDATE scheduled_messages
                   SET status=?, result=?, error=?, sent_at=?, updated_at=?
                   WHERE id=? AND status NOT IN ('sent','cancelled')""",
                (row.get("status"), row.get("result"), row.get("error"),
                 row.get("sent_at"), row.get("updated_at"), row["id"]),
            )
            updated += 1
        except Exception:
            pass
    conn.commit()

    if max_ts > hwm:
        _set_hwm("sched_msg_pull_hwm", max_ts)
    return updated


def _push_files(cfg: SyncConfig) -> bool:
    """Push staging, birthday_window, last_executor_check to VPS.

    Note: staging is rsynced *without* --delete. Previous behaviour used
    --delete to clean up processed files, but that races with newly-staged
    VPS files: if a Telegram drop arrives between sync cycles, the push
    step can wipe the VPS staging dir before the next cycle's pull step
    fetches it, leaving a queue_item with `media_staging_path` pointing
    at a now-deleted file. VPS-side staging cleanup is handled separately
    (or just left to accumulate — files are small).
    """
    ok = True
    staging_local = f"{cfg.config_dir}/data/staging/"
    os.makedirs(staging_local, exist_ok=True)
    staging_remote = f"{cfg.vps_host}:{cfg.vps_config_root}/data/staging/"
    if not _rsync(cfg, staging_local, staging_remote, delete=False):
        ok = False

    # Birthday window
    bday = Path(cfg.config_dir) / "data" / "contacts" / "birthday_window.json"
    if bday.exists():
        _ssh_run(cfg, f"mkdir -p {cfg.vps_config_root}/data/contacts")
        if not _rsync(cfg, str(bday), f"{cfg.vps_host}:{cfg.vps_config_root}/data/contacts/birthday_window.json"):
            ok = False

    # references.yaml — global tag/route config, sensor needs it for note routing
    refs = Path(cfg.config_dir) / "config" / "references.yaml"
    if refs.exists():
        _ssh_run(cfg, f"mkdir -p {cfg.vps_config_root}/config")
        if not _rsync(cfg, str(refs), f"{cfg.vps_host}:{cfg.vps_config_root}/config/references.yaml"):
            ok = False

    return ok


def _write_last_check(cfg: SyncConfig) -> None:
    """Write and push executor heartbeat timestamp."""
    ts = _now()
    check_file = Path(cfg.config_dir) / "logs" / "last_executor_check"
    check_file.parent.mkdir(parents=True, exist_ok=True)
    check_file.write_text(ts)
    _rsync(cfg, str(check_file),
           f"{cfg.vps_host}:{cfg.vps_config_root}/logs/last_executor_check")


# ── Main entry point ─────────────────────────────────────────────────────────


def run_sync_cycle(cfg: SyncConfig) -> SyncStats:
    """Run one full sync cycle. Non-fatal: each step can fail independently."""
    t0 = time.monotonic()
    stats = SyncStats()

    # 0. Reachability check
    try:
        r = _ssh_run(cfg, "echo ok", timeout=cfg.ssh_timeout + 5)
        if r.returncode != 0 or "ok" not in r.stdout:
            stats.duration_ms = int((time.monotonic() - t0) * 1000)
            return stats
    except (subprocess.TimeoutExpired, OSError):
        stats.duration_ms = int((time.monotonic() - t0) * 1000)
        return stats

    # 1. Pull new queue rows from VPS
    try:
        stats.pulled = _pull_queue_rows(cfg)
    except Exception as e:
        print(f"[sync] pull rows error: {e}")

    # 2. Pull files (staging, lists, notes)
    try:
        stats.files_ok = _pull_files(cfg)
    except Exception as e:
        print(f"[sync] pull files error: {e}")
        stats.files_ok = False

    # 3. Push status updates to VPS
    try:
        stats.pushed = _push_status_updates(cfg)
    except Exception as e:
        print(f"[sync] push status error: {e}")

    # 3b. Push agent_reply_requests to VPS — MUST be before outbox push so VPS
    #     has the reply request in its DB before flush_outbox.py sends the message.
    try:
        stats.reply_reqs = _push_reply_requests(cfg)
    except Exception as e:
        print(f"[sync] push reply_requests error: {e}")

    # 4. Push outbox items to VPS
    try:
        stats.outbox = _push_outbox(cfg)
    except Exception as e:
        print(f"[sync] push outbox error: {e}")

    # 4c. Push signal heartbeats so the VPS watchdog can see which Mac jobs ran.
    try:
        _push_heartbeats(cfg)
    except Exception as e:
        print(f"[sync] push heartbeats error: {e}")

    # 4b. Pull agent_replies from VPS so gateway can serve them to polling agents.
    try:
        stats.replies_pulled = _pull_agent_replies(cfg)
    except Exception as e:
        print(f"[sync] pull agent_replies error: {e}")

    # 4c. Push scheduled_messages to VPS; pull results back.
    try:
        _push_scheduled_messages(cfg)
    except Exception as e:
        print(f"[sync] push scheduled_messages error: {e}")
    try:
        _pull_scheduled_message_results(cfg)
    except Exception as e:
        print(f"[sync] pull scheduled_message results error: {e}")

    # 5. Push files back (staging --delete, birthday)
    try:
        if not _push_files(cfg):
            stats.files_ok = False
    except Exception as e:
        print(f"[sync] push files error: {e}")
        stats.files_ok = False

    # 5b. Bidirectional tasks.json merge
    try:
        if not _sync_tasks(cfg):
            stats.files_ok = False
    except Exception as e:
        print(f"[sync] tasks merge error: {e}")
        stats.files_ok = False

    # 6. Heartbeat
    try:
        _write_last_check(cfg)
    except Exception as e:
        print(f"[sync] heartbeat error: {e}")

    # 7. Update last_sync_at
    try:
        _set_hwm("last_sync_at", _now())
    except Exception:
        pass

    stats.duration_ms = int((time.monotonic() - t0) * 1000)
    return stats
