#!/usr/bin/env python3
# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0
"""
executor/console/server.py — Aaka Console unified server (port 8003).

Serves three tabs (Console · Tasks · Status) from a single process.
  • Reuses POST /webui/messages, GET /webui/stream, POST /webui/upload,
    GET /webui/state from executor/webui/server.py (imported, not duplicated).
  • Mounts taskboard.api.router at /tasks (→ /tasks/api/*).
  • Adds GET /console/status/data, GET /console/status/logs.
  • Serves GET / → shell HTML, GET /healthz → {"ok": true}.

The existing executor/webui/server.py is NOT modified — this module imports its
handler functions and re-registers them on a fresh FastAPI app. State lives in
webui.State (class attributes, per-process), set from --config at startup.

Run:
  /Users/Shared/aaka-repo/venv/bin/python3 \\
      executor/console/server.py --config /Users/Shared/aaka-repo-config --port 8003
  # or via uvicorn CLI (launchd):
  uvicorn executor.console.server:app --host 127.0.0.1 --port 8003
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONSOLE_DIR = Path(__file__).resolve().parent
CONSOLE_STATIC = CONSOLE_DIR / "static"
WEBUI_DIR = REPO_ROOT / "executor" / "webui"
BRAND_DIR = WEBUI_DIR / "brand"
TASKBOARD_STATIC = REPO_ROOT / "taskboard" / "static"

sys.path.insert(0, str(REPO_ROOT))

# Import the webui app's handler functions (message path) — reused verbatim.
from executor.webui import server as webui  # noqa: E402
# Reuse the webui State object so status/logs + the message handlers share config.
State = webui.State  # noqa: N816

# Mount the taskboard CRUD router.
from taskboard.api import router as tasks_router  # noqa: E402

# When launched via the uvicorn CLI (launchd), there is no main() to set config
# from --config. Honor AAKA_CONFIG_DIR from the environment at import time so
# State + the status/logs routes resolve correctly. main() overrides this for
# the direct-invocation path.
_env_config = os.environ.get("AAKA_CONFIG_DIR")
if _env_config:
    _cfg = Path(_env_config).expanduser()
    if _cfg.exists():
        State.config_dir = _cfg.resolve()
        State.mode = webui._detect_mode(State.config_dir)

app = FastAPI(title="Aaka Console")


# ── Console-specific routes ───────────────────────────────────────────────────
@app.get("/")
def root():
    return FileResponse(CONSOLE_STATIC / "console.html")


@app.get("/healthz")
def healthz():
    return {"ok": True, "mode": getattr(State, "mode", "live")}


@app.get("/console/status/data")
async def status_data():
    """Run admin/setup_check.py --json as a subprocess; return its JSON.

    Read-only. Times out at 8s; non-zero exit and timeouts return an
    {"error": ...} object so the browser can render a single error card.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(REPO_ROOT / "admin" / "setup_check.py"),
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "AAKA_CONFIG_DIR": str(State.config_dir)},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        if proc.returncode == 0:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError as exc:
                return {"error": f"could not parse setup_check output: {exc}"}
        return {"error": f"setup_check exited {proc.returncode}",
                "stderr": (stderr.decode(errors="replace") or "")[:500]}
    except asyncio.TimeoutError:
        return {"error": "timeout — setup_check.py took longer than 8s"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": str(exc)}


@app.get("/console/status/logs")
def status_logs():
    """Return the last 20 lines of the queue-worker and sensor logs.

    Missing files return empty arrays (never an error). Read-only.
    """
    def tail(path: Path, n: int = 20) -> list[str]:
        if not path.exists():
            return []
        try:
            return path.read_text(errors="replace").splitlines()[-n:]
        except Exception:
            return []

    logs_dir = State.config_dir / "logs"
    return {
        "queueworker": tail(logs_dir / "queueworker.log"),
        "sensor": tail(logs_dir / "telegram_poller.log"),
    }


# ── Reused webui message-path routes (imported handlers, no logic copy) ───────
app.add_api_route("/webui/state", webui.state, methods=["GET"])
app.add_api_route("/webui/messages", webui.send_message, methods=["POST"],
                  response_model=webui.SendOut)
app.add_api_route("/webui/upload", webui.upload, methods=["POST"])
app.add_api_route("/webui/stream", webui.stream, methods=["GET"])

# ── Tasks router (mounted at /tasks → /tasks/api/*) ───────────────────────────
app.include_router(tasks_router, prefix="/tasks")


@app.get("/tasks/")
def tasks_frame():
    return FileResponse(CONSOLE_STATIC / "tasks-frame.html")


# ── Static files ──────────────────────────────────────────────────────────────
# Order matters: more-specific mounts must be registered first. The taskboard's
# app.js + original style.css are served under /tasks/static/ for the iframe; the
# brand + console-scoped CSS under /static/brand and /static.
app.mount("/tasks/static", StaticFiles(directory=TASKBOARD_STATIC), name="tasks-static")
app.mount("/static/brand", StaticFiles(directory=BRAND_DIR), name="brand")
app.mount("/static", StaticFiles(directory=CONSOLE_STATIC), name="static")


# ── Startup: load identity so /webui/state + /webui/messages work ─────────────
@app.on_event("startup")
def _startup():
    webui._reload_identity()
    print(f"Aaka Console · config={State.config_dir}", file=sys.stderr)
    print(f"  members: {list(State.members)}", file=sys.stderr)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="Aaka Console server")
    p.add_argument(
        "--config",
        default=os.environ.get("AAKA_CONFIG_DIR") or str(REPO_ROOT / "samples" / "demo"),
    )
    p.add_argument("--port", type=int, default=8003)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()

    config_dir = Path(args.config).expanduser().resolve()
    if not config_dir.exists():
        print(f"ERROR: config dir not found: {config_dir}", file=sys.stderr)
        return 1

    os.environ["AAKA_CONFIG_DIR"] = str(config_dir)
    if not os.environ.get("QUEUE_DB"):
        os.environ["QUEUE_DB"] = str(config_dir / "data" / "queue" / "butler.db")
    State.config_dir = config_dir
    State.mode = webui._detect_mode(config_dir)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port,
                log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
