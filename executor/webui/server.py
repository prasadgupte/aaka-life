#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0
"""
executor/webui/server.py — Aaka local web channel + cassette host.

Runs on the executor (Mac, localhost). Real bidirectional channel:
  • Inbound POST /webui/messages → sensor.route() with channel="web"
  • Inbound POST /webui/upload → stages file + drop_file intent
  • Outbound GET /webui/stream?session=<sid> → SSE polling outbox_items
    WHERE source='web' AND channel_id='web:<sid>'.

Mode is set at startup via --config:
  --config samples/demo                    → demo (Ash-Kaa sample family)
  --config /Users/Shared/aaka-repo-config  → live

Run:
  /Users/Shared/aaka-repo/venv/bin/python3 \\
      executor/webui/server.py --config samples/demo --port 18791
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WEBUI_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEBUI_DIR / "static"
CASSETTE_DIR = WEBUI_DIR / "cassettes"
MANIFEST = CASSETTE_DIR / "manifest.json"

# Single lock around env mutation + sensor.route call (route is blocking and
# reads AAKA_CONFIG_DIR globally).
_route_lock = threading.Lock()


# ── Models ────────────────────────────────────────────────────────────────────
class SendIn(BaseModel):
    text: str
    member_id: str
    session_id: str
    group: str = ""              # purpose-bound group name, e.g. "notes" or "files"


class SendOut(BaseModel):
    reply: str
    ts_ms: int
    latency_ms: int


class SaveCassetteIn(BaseModel):
    title: str
    summary: str = ""
    messages: list[dict]


# ── State ────────────────────────────────────────────────────────────────────
class State:
    config_dir: Path = REPO_ROOT / "samples" / "demo"
    mode: str = "demo"
    bot: dict = {}
    members: dict = {}


# ── Helpers ──────────────────────────────────────────────────────────────────
def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s or "untitled"


def _safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._-")
    return s or "file"


def _reload_identity() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    if "aaka_config" in sys.modules:
        import importlib
        importlib.reload(sys.modules["aaka_config"])
    import aaka_config  # noqa: F401
    State.bot = {"name": aaka_config.bot_name(), "emoji": aaka_config.bot_emoji()}
    State.members = {
        m["id"]: {
            "name": m.get("name") or m["id"].capitalize(),
            "telegram": str(m.get("telegram") or ""),
            "emoji": m.get("emoji") or "",
            "role": m.get("role") or "",
            "admin": bool(m.get("admin", False)),
        }
        for m in aaka_config.members()
    }


def _channel_id_for(group: str, session_id: str) -> str:
    """Build the channel_id used in metadata + outbox routing.

    When the user is in a named group (notes/files/notifications), the
    channel_id is "web:<group>" so the sensor's group_for() lookup
    matches. Otherwise it's a per-session id so each tab gets its own
    routing lane.
    """
    if group:
        return f"web:{group}"
    return f"web:{session_id}"


def _format_a_wrap(
    message_text: str,
    sender_telegram: str,
    session_id: str,
    member_id: str,
    group: str = "",
    media_path: str | None = None,
    media_mime: str | None = None,
) -> str:
    """Build a Format-A sensor input.

    The sensor parses:
      - `[media attached: <path> (<mime>)]` (search anywhere in raw)
      - ```json {...} ``` metadata block
      - everything after the last ``` is the user message

    We tag channel='web' so source flows through the queue and outbox.
    """
    cid = _channel_id_for(group, session_id)
    meta = {
        "channel": "web",
        "sender_id": sender_telegram,
        # router_sensor reads `channel_id` from meta (line 1339);
        # `chat_id` is only consulted for channel inference.
        "channel_id": cid,
        "chat_id": cid,
        "source_member": member_id,
    }
    parts: list[str] = []
    if media_path:
        parts.append(f"[media attached: {media_path} ({media_mime or 'application/octet-stream'})]")
    parts.append("Conversation info (untrusted metadata):")
    parts.append("```json")
    parts.append(json.dumps(meta))
    parts.append("```")
    parts.append("")
    parts.append(message_text or "")
    return "\n".join(parts)


def _drive_sensor(raw_input: str) -> str:
    """Call sensor.route() in-process. Thread-safe."""
    with _route_lock:
        if "sensor.router_sensor" not in sys.modules:
            sys.path.insert(0, str(REPO_ROOT))
        from sensor.router_sensor import route
        return (route(raw_input, dry_run=False) or "").rstrip()


def _validate_member(member_id: str) -> str:
    if member_id not in State.members:
        raise HTTPException(400, f"unknown member: {member_id}")
    sender_tg = State.members[member_id]["telegram"]
    if not sender_tg:
        raise HTTPException(400, f"member {member_id} has no telegram id in config")
    return sender_tg


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Aaka WebUI")


@app.on_event("startup")
def _startup():
    _reload_identity()
    # Demo mode: regenerate calendar + tasks with today's actual dates so
    # /today, /week, /tasks etc. don't show events from when the fixtures
    # were authored.
    if State.mode == "demo":
        try:
            sys.path.insert(0, str(REPO_ROOT / "samples" / "demo"))
            from refresh import refresh as _demo_refresh
            _demo_refresh(verbose=False)
            print("  demo data: refreshed", file=sys.stderr)
        except Exception as exc:
            print(f"  demo data: refresh failed: {exc}", file=sys.stderr)
    print(f"Aaka WebUI · mode={State.mode} · config={State.config_dir}", file=sys.stderr)
    print(f"  bot:     {State.bot.get('emoji')} {State.bot.get('name')}", file=sys.stderr)
    print(f"  members: {list(State.members)}", file=sys.stderr)


@app.get("/")
def root():
    return RedirectResponse("/static/live.html")


@app.get("/healthz")
def health():
    return {"ok": True, "mode": State.mode}


@app.get("/webui/state")
def state():
    """Return identity + a fresh session_id. Browser persists this in localStorage."""
    sys.path.insert(0, str(REPO_ROOT))
    import aaka_config
    groups_summary = {
        gid: {
            "description": g.get("description", ""),
            "purpose": g.get("purpose", ""),
            "default_intent": g.get("default_intent"),
        }
        for gid, g in aaka_config.groups().items()
    }
    return {
        "mode": State.mode,
        "config_dir": str(State.config_dir),
        "bot": State.bot,
        "members": State.members,
        "groups": groups_summary,
        "session_id": uuid.uuid4().hex[:16],
    }


@app.post("/webui/messages", response_model=SendOut)
def send_message(body: SendIn):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    sender_tg = _validate_member(body.member_id)
    raw = _format_a_wrap(text, sender_tg, body.session_id, body.member_id,
                         group=body.group)
    t0 = time.monotonic()
    try:
        reply = _drive_sensor(raw)
    except Exception as exc:
        raise HTTPException(500, f"sensor error: {exc}")
    latency = int((time.monotonic() - t0) * 1000)
    return SendOut(reply=reply, ts_ms=int(time.time() * 1000), latency_ms=latency)


@app.post("/webui/upload")
async def upload(
    file: UploadFile = File(...),
    member_id: str = Form(...),
    session_id: str = Form(...),
    text: str = Form(""),
    group: str = Form(""),
):
    sender_tg = _validate_member(member_id)

    staging_root = State.config_dir / "data" / "staging" / f"web-{uuid.uuid4().hex[:12]}"
    staging_root.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(file.filename or "upload")
    dest = staging_root / safe

    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    mime = file.content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream"
    caption = (text or "").strip() or "f"  # bare 'f' triggers smart-drop guess
    raw = _format_a_wrap(caption, sender_tg, session_id, member_id,
                         group=group,
                         media_path=str(dest), media_mime=mime)

    t0 = time.monotonic()
    try:
        reply = _drive_sensor(raw)
    except Exception as exc:
        raise HTTPException(500, f"sensor error: {exc}")
    latency = int((time.monotonic() - t0) * 1000)
    return {
        "reply": reply,
        "ts_ms": int(time.time() * 1000),
        "latency_ms": latency,
        "filename": safe,
        "size_bytes": dest.stat().st_size,
        "staged_path": str(dest),
    }


# ── SSE outbox stream ─────────────────────────────────────────────────────────
async def _stream_outbox(channel_id: str, request: Request):
    """SSE generator. Polls outbox_items for matching web rows every 500 ms.

    Each yielded row is marked status='sent' so it never gets re-delivered.
    Heartbeat every 30 s to keep the connection alive through proxies.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from aaka_queue.queue import _connect

    yield ": aaka stream open\n\n"
    last_hb = time.monotonic()

    while True:
        if await request.is_disconnected():
            break

        # Poll DB
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT id, text, reply_markup, source, channel_id, created_at "
                    "FROM outbox_items "
                    "WHERE source = 'web' AND channel_id = ? AND status = 'pending' "
                    "ORDER BY created_at LIMIT 50",
                    (channel_id,),
                ).fetchall()
                for r in rows:
                    rid = r["id"]
                    payload = {
                        "id": rid,
                        "text": r["text"],
                        "channel_id": r["channel_id"],
                        "created_at": r["created_at"],
                    }
                    if r["reply_markup"]:
                        try:
                            payload["reply_markup"] = json.loads(r["reply_markup"])
                        except json.JSONDecodeError:
                            pass
                    yield f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    conn.execute(
                        "UPDATE outbox_items SET status='sent' WHERE id=?",
                        (rid,),
                    )
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

        # Heartbeat
        if time.monotonic() - last_hb > 30:
            yield ": ping\n\n"
            last_hb = time.monotonic()

        await asyncio.sleep(0.5)


@app.get("/webui/stream")
async def stream(session: str, request: Request, group: str = ""):
    """SSE stream of outbox rows for this session (or group, if set)."""
    if not session and not group:
        raise HTTPException(400, "missing session id")
    # When in a named group, subscribe to web:<group> so notifications +
    # group-targeted deliveries land here.
    channel_id = f"web:{group}" if group else f"web:{session}"
    return StreamingResponse(
        _stream_outbox(channel_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Cassette save (unchanged from v1) ────────────────────────────────────────
@app.post("/webui/cassettes")
def save_cassette(body: SaveCassetteIn):
    if not body.title.strip():
        raise HTTPException(400, "title is required")
    if not body.messages:
        raise HTTPException(400, "messages list is empty")

    cassette = {
        "schema": "aaka-cassette/v0",
        "title": body.title.strip(),
        "summary": body.summary.strip(),
        "bot": State.bot,
        "members": State.members,
        "messages": body.messages,
    }
    name = _slug(body.title)
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    path = CASSETTE_DIR / f"{name}.json"
    path.write_text(
        json.dumps(cassette, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    entries = []
    if MANIFEST.exists():
        try:
            entries = json.loads(MANIFEST.read_text())
        except json.JSONDecodeError:
            entries = []
    entries = [e for e in entries if e.get("name") != name]
    last = body.messages[-1].get("ts_offset_ms", 0) if body.messages else 0
    entries.append({
        "name": name,
        "title": cassette["title"],
        "summary": cassette["summary"],
        "duration_estimate_ms": last + 1500,
    })
    entries.sort(key=lambda e: e["name"])
    MANIFEST.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "name": name,
        "path": str(path.relative_to(REPO_ROOT)),
        "play_url": f"/static/index.html?play={name}&autoplay=1",
    }


@app.get("/webui/cassettes")
def list_cassettes():
    if not MANIFEST.exists():
        return []
    try:
        return json.loads(MANIFEST.read_text())
    except json.JSONDecodeError:
        return []


@app.get("/cassettes/{name}.json")
def get_cassette(name: str):
    path = CASSETTE_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, "cassette not found")
    return FileResponse(path, media_type="application/json")


@app.get("/cassettes/manifest.json")
def get_manifest():
    if not MANIFEST.exists():
        return []
    return FileResponse(MANIFEST, media_type="application/json")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Entry point ──────────────────────────────────────────────────────────────
def _detect_mode(config_dir: Path) -> str:
    samples = REPO_ROOT / "samples" / "demo"
    try:
        return "demo" if config_dir.resolve() == samples.resolve() else "live"
    except OSError:
        return "live"


def main() -> int:
    p = argparse.ArgumentParser(description="Aaka WebUI server")
    p.add_argument(
        "--config",
        default=os.environ.get("AAKA_CONFIG_DIR") or str(REPO_ROOT / "samples" / "demo"),
    )
    p.add_argument("--port", type=int, default=18791)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()

    config_dir = Path(args.config).expanduser().resolve()
    if not config_dir.exists():
        print(f"ERROR: config dir not found: {config_dir}", file=sys.stderr)
        return 1

    os.environ["AAKA_CONFIG_DIR"] = str(config_dir)
    # queue.py reads QUEUE_DB at import time. Default it to config_dir/data/queue/butler.db
    # so the sensor + executor both see the same DB this server is polling for SSE.
    if not os.environ.get("QUEUE_DB"):
        os.environ["QUEUE_DB"] = str(config_dir / "data" / "queue" / "butler.db")
    State.config_dir = config_dir
    State.mode = _detect_mode(config_dir)

    import uvicorn
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
