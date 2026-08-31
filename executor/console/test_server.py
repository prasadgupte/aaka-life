#!/usr/bin/env python3
"""
executor/console/test_server.py — route + structure tests for the console server.

No network: uses starlette/fastapi TestClient against the imported app object.
Run: /Users/Shared/aaka-repo/venv/bin/python3 executor/console/test_server.py
     (exit 0 = pass)

The console server extends the webui app (imports its message-path handlers) and
mounts the taskboard router at /tasks. These tests confirm the routes are wired,
the shell HTML is served, and the tasks CRUD reaches the same local_tasks store.
"""
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Point config at a throwaway dir BEFORE importing the server so aaka_config and
# the taskboard store resolve to an isolated location (no live data touched).
_TMP_CONFIG = Path(tempfile.mkdtemp(prefix="aaka-console-test-"))
(_TMP_CONFIG / "config").mkdir(parents=True, exist_ok=True)
(_TMP_CONFIG / "data" / "queue").mkdir(parents=True, exist_ok=True)
(_TMP_CONFIG / "logs").mkdir(parents=True, exist_ok=True)
# Minimal aaka.yaml so aaka_config loads cleanly.
(_TMP_CONFIG / "config" / "aaka.yaml").write_text(
    "timezone: Europe/Berlin\n"
    "members:\n"
    "  - id: kid\n"
    "    name: Kid One\n"
    "    role: child\n"
    "    telegram: '111'\n"
    "  - id: alex\n"
    "    name: Alex Admin\n"
    "    role: adult\n"
    "    admin: true\n"
    "    telegram: '222'\n",
    encoding="utf-8",
)
os.environ["AAKA_CONFIG_DIR"] = str(_TMP_CONFIG)
os.environ["AAKA_BASE"] = str(REPO_ROOT)
os.environ["QUEUE_DB"] = str(_TMP_CONFIG / "data" / "queue" / "butler.db")

from fastapi.testclient import TestClient  # noqa: E402

from executor.console import server as console  # noqa: E402

# Ensure the server's State points at the throwaway config for status/logs.
console.State.config_dir = _TMP_CONFIG
# The startup event (which calls _reload_identity) only fires under a `with`
# TestClient; load identity explicitly so /webui/state returns our members.
console.webui._reload_identity()

client = TestClient(console.app)

_FAILURES = []


def check(desc, cond):
    print(("  ok  " if cond else " FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


def _route_methods(path: str) -> set:
    methods = set()
    for r in console.app.routes:
        if getattr(r, "path", None) == path:
            methods |= set(getattr(r, "methods", set()) or set())
    return methods


def _has_route(path: str, method: str) -> bool:
    return method in _route_methods(path)


CONSOLE_HTML = (Path(__file__).resolve().parent / "static" / "console.html").read_text(encoding="utf-8")
TASKS_CSS = (Path(__file__).resolve().parent / "static" / "tasks.css").read_text(encoding="utf-8")
PLIST = (REPO_ROOT / "executor" / "com.aaka.console.plist").read_text(encoding="utf-8")
LAUNCHER = REPO_ROOT / "admin" / "launch_console.sh"


def main():
    # ── Shell ──────────────────────────────────────────────────────────────
    r = client.get("/")
    check("GET / returns 200", r.status_code == 200)
    body = r.text
    check("GET / body contains console-tab", "console-tab" in body)
    check("GET / body is dark themed", 'data-theme="dark"' in body)

    # tab hash routing structure (parse the served HTML)
    for tab in ("console", "tasks", "status"):
        check(f'shell has data-tab="{tab}"', f'data-tab="{tab}"' in CONSOLE_HTML)
        check(f"shell has #panel-{tab}", f'id="panel-{tab}"' in CONSOLE_HTML)

    r = client.get("/healthz")
    check("GET /healthz returns 200", r.status_code == 200)
    check("GET /healthz ok:true", r.json().get("ok") is True)

    # ── Console tab (route registration + HTML content) ────────────────────
    check("POST /webui/messages registered", _has_route("/webui/messages", "POST"))
    check("GET /webui/stream registered", _has_route("/webui/stream", "GET"))
    check("GET /webui/state registered", _has_route("/webui/state", "GET"))
    check("POST /webui/upload registered", _has_route("/webui/upload", "POST"))

    # Markdown renderer wired into the console (change 1)
    check("md.js loaded in shell", "/static/md.js" in CONSOLE_HTML)
    check("md.js vendored into console static",
          (Path(__file__).resolve().parent / "static" / "md.js").exists())
    CONSOLE_JS = (Path(__file__).resolve().parent / "static" / "console.js").read_text(encoding="utf-8")
    check("console.js calls renderMd for bot bubbles",
          "renderMd" in CONSOLE_JS and "renderBubbleText" in CONSOLE_JS)
    MD_JS = (Path(__file__).resolve().parent / "static" / "md.js").read_text(encoding="utf-8")
    check("md.js sanitizes URLs (no javascript:/data:)", "_mdSafeUrl" in MD_JS)
    check("md.js opens links in a new tab", 'target="_blank"' in MD_JS)
    check("md.js renders images", "md-img" in MD_JS)

    # ── Sender: default-admin + avatar chips (changes 2 & 3) ───────────────
    r = client.get("/webui/state")
    members = r.json().get("members", {})
    check("state exposes member role", "role" in members.get("alex", {}))
    check("state exposes member admin flag", members.get("alex", {}).get("admin") is True)
    check("non-admin member has admin:false", members.get("kid", {}).get("admin") is False)
    check("old <select> member picker removed", 'id="console-member"' not in CONSOLE_HTML)
    check("avatar roster container present", 'id="console-roster"' in CONSOLE_HTML)
    check("console.js builds avatar chips", "console-ava-chip" in CONSOLE_JS)
    check("console.js pre-selects admin sender",
          ".admin" in CONSOLE_JS and "activeMember" in CONSOLE_JS)

    # ── Command menu replaces always-on pills (change 4) ───────────────────
    check("old always-on chips row removed", 'id="console-chips"' not in CONSOLE_HTML)
    check("(+) command toggle present", 'id="console-cmd-toggle"' in CONSOLE_HTML)
    check("command menu container present", 'id="console-cmd-menu"' in CONSOLE_HTML)
    check("console.js has command list", "COMMANDS" in CONSOLE_JS and "/today" in CONSOLE_JS)
    check("slash typing opens menu", "startsWith('/')" in CONSOLE_JS)
    check("command menu is keyboard-navigable",
          "ArrowDown" in CONSOLE_JS and "moveCmdSel" in CONSOLE_JS)

    # ── Status tab HTML / JS presence ──────────────────────────────────────
    check("renderCard function present in console.js", "renderCard" in CONSOLE_JS)
    check("status-fix class referenced in HTML/JS", "status-fix" in CONSOLE_HTML or "status-fix" in CONSOLE_JS)

    # Status routes are read-only: no mutating methods under /console/status/
    mutating = {"POST", "PATCH", "DELETE", "PUT"}
    status_mutating = []
    for rt in console.app.routes:
        p = getattr(rt, "path", "") or ""
        if p.startswith("/console/status/"):
            if mutating & set(getattr(rt, "methods", set()) or set()):
                status_mutating.append(p)
    check("no mutating routes under /console/status/", not status_mutating)

    # ── Tasks tab (mounted taskboard router) ───────────────────────────────
    r = client.get("/tasks/api/tasks")
    check("GET /tasks/api/tasks returns 200", r.status_code == 200)
    check("tasks list response has 'tasks' key", "tasks" in r.json())

    # Create + complete a task through the mounted router
    r = client.post("/tasks/api/tasks", json={"title": "console-test-task"})
    check("POST /tasks/api/tasks returns 201", r.status_code == 201)
    tid = r.json().get("id")
    check("created task has an id", bool(tid))

    r = client.patch(f"/tasks/api/tasks/{tid}", json={"status": "done"})
    check("PATCH task status=done returns 200", r.status_code == 200)

    # Confirm it left the open list
    r = client.get("/tasks/api/tasks")
    open_ids = {t["id"] for t in r.json().get("tasks", [])}
    check("completed task no longer in open list", tid not in open_ids)

    # Snooze path
    r = client.post("/tasks/api/tasks", json={"title": "snooze-me"})
    sid = r.json()["id"]
    r = client.patch(f"/tasks/api/tasks/{sid}", json={"snooze_days": 1})
    check("PATCH snooze_days=1 returns 200", r.status_code == 200)
    r = client.get("/tasks/api/tasks")
    check("snoozed task removed from open list", sid not in {t["id"] for t in r.json().get("tasks", [])})

    # Delete path
    r = client.post("/tasks/api/tasks", json={"title": "delete-me"})
    did = r.json()["id"]
    r = client.delete(f"/tasks/api/tasks/{did}")
    check("DELETE task returns 204", r.status_code == 204)

    # Bulk action
    a = client.post("/tasks/api/tasks", json={"title": "bulk-a"}).json()["id"]
    b = client.post("/tasks/api/tasks", json={"title": "bulk-b"}).json()["id"]
    r = client.post("/tasks/api/tasks/bulk-action", json={"ids": [a, b], "action": "done"})
    check("bulk-action done affects 2", r.status_code == 200 and r.json().get("affected") == 2)

    # ── Tasks reskin token map ─────────────────────────────────────────────
    check("tasks.css remaps to --aaka-teal", "var(--aaka-teal)" in TASKS_CSS)
    check("tasks.css remaps to --aaka-iris", "var(--aaka-iris)" in TASKS_CSS)
    check("tasks.css remaps to --aaka-berry", "var(--aaka-berry)" in TASKS_CSS)

    # ── Launcher + plist ───────────────────────────────────────────────────
    check("plist references console module path", "executor.console.server:app" in PLIST)
    check("launch_console.sh exists", LAUNCHER.exists())
    check("launch_console.sh is executable", os.access(LAUNCHER, os.X_OK))

    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all console server checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
