#!/usr/bin/env python3
"""
sensor/tool_runner.py — aaka Tools runtime: run a registered tool, capture its
structured result, log it, and report back through aaka (esp. errors).

A tool is a manifest entry in $AAKA_CONFIG_DIR/config/tools.yaml:

  homework:
    run: /Users/Shared/aaka-repo/tools/webuntis/check.py
    args: "--days 7"
    placement: sensor            # sensor | executor  (informational here)
    schedule: "0 7 * * *"
    command: "/homework"
    report_to: <member-id>
    on_error: alert              # alert | digest | silent
    secrets: webuntis            # → $AAKA_CONFIG_DIR/secrets/webuntis (or absolute)
    enabled: true

The tool prints one JSON line: {"ok", "summary", "details", "error"} and exits
0/non-zero. This runner is shared by the sensor cron and the executor dispatcher;
`placement` only decides which side schedules it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parents[1])
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import aaka_config  # noqa: E402


def _config_dir() -> Path:
    return Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))


def load_manifest() -> dict:
    """Tool manifest from config/tools.yaml (empty if absent)."""
    import yaml
    p = _config_dir() / "config" / "tools.yaml"
    try:
        return yaml.safe_load(p.read_text()) or {} if p.exists() else {}
    except Exception:
        return {}


def _secrets_dir(entry: dict) -> str:
    s = entry.get("secrets") or ""
    if not s:
        return ""
    if os.path.isabs(s):
        return s
    return str(_config_dir() / "secrets" / s)


def _log(name: str, record: dict) -> None:
    p = _config_dir() / "logs" / "tools" / f"{name}.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
        with open(p, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def run_tool(name: str, entry: dict | None = None, extra_args: str = "") -> dict:
    """Execute one tool. Returns the structured result dict (never raises)."""
    entry = entry or load_manifest().get(name) or {}
    run = entry.get("run")
    if not run:
        return {"ok": False, "error": "no_such_tool", "summary": f"tool '{name}' has no run target"}

    cmd = [sys.executable, run] + (entry.get("args", "").split() if entry.get("args") else [])
    if extra_args:
        cmd += extra_args.split()
    env = dict(os.environ)
    sec = _secrets_dir(entry)
    if sec:
        env["AAKA_TOOL_SECRETS"] = sec

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=entry.get("timeout", 120), env=env)
        raw = (proc.stdout or "").strip().splitlines()
        result = json.loads(raw[-1]) if raw else {}
        if not isinstance(result, dict) or "ok" not in result:
            result = {"ok": proc.returncode == 0,
                      "summary": (proc.stdout or proc.stderr or "").strip()[:400],
                      "error": None if proc.returncode == 0 else "bad_output"}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "timeout", "summary": f"{name} timed out"}
    except Exception as e:
        result = {"ok": False, "error": "runner_error", "summary": str(e)[:300]}

    result.setdefault("summary", "")
    result.setdefault("details", "")
    result.setdefault("error", None)
    _log(name, {"ok": result.get("ok"), "error": result.get("error"),
                "summary": result.get("summary", "")[:200], "ms": int((time.time() - t0) * 1000)})
    return result


def _send(member_id: str, text: str) -> None:
    """Deliver a report to a member via their preferred channel (best-effort)."""
    try:
        from message_send import send
        m = next((x for x in aaka_config.members() if x.get("id") == member_id), None)
        if not m:
            return
        tg = m.get("telegram_id") or m.get("telegram")
        if tg:
            send(text, channel="telegram", to=str(tg), silent=True)
    except Exception:
        pass


def report(name: str, entry: dict, result: dict) -> None:
    """Deliver the run result per the manifest (never silent on error)."""
    report_to = entry.get("report_to") or aaka_config.default_actor()
    bot = ""
    if result.get("ok"):
        _send(report_to, result.get("summary") or f"✅ {name}: done.")
        return
    err = result.get("error")
    on_error = entry.get("on_error", "alert")
    if on_error == "silent":
        return
    admin = next((m["id"] for m in aaka_config.members() if m.get("admin")), report_to)
    msg = f"⚠️ Tool *{name}* failed ({err}): {result.get('summary', '')}"
    if err == "auth_required":
        msg = (f"🔐 Tool *{name}* needs a re-auth: {result.get('summary', '')}\n"
               f"Update its credentials in the vault, then it'll resume next run.")
    _send(admin, msg)


def run_and_report(name: str, extra_args: str = "") -> dict:
    entry = load_manifest().get(name) or {}
    if entry and not entry.get("enabled", True):
        return {"ok": False, "error": "disabled", "summary": f"{name} is disabled"}
    result = run_tool(name, entry, extra_args)
    report(name, entry, result)
    return result


def run_due(now_min: int | None = None) -> list:
    """Run every enabled tool whose cron schedule matches now (called by cron)."""
    from datetime import datetime
    ran = []
    for name, entry in load_manifest().items():
        if not entry.get("enabled", True) or not entry.get("schedule"):
            continue
        if _cron_matches(entry["schedule"], datetime.now()):
            ran.append((name, run_and_report(name)))
    return ran


def _cron_matches(expr: str, when) -> bool:
    """Minimal 5-field cron match (min hour dom mon dow); '*' and lists/ranges/steps."""
    fields = expr.split()
    if len(fields) != 5:
        return False
    # cron dow: 0=Sun..6=Sat; python weekday: 0=Mon..6=Sun → (wd+1)%7
    vals = [when.minute, when.hour, when.day, when.month, (when.weekday() + 1) % 7]
    for f, v in zip(fields, vals):
        if not _cron_field(f, v):
            return False
    return True


def _cron_field(f: str, v: int) -> bool:
    if f == "*":
        return True
    for part in f.split(","):
        if part.startswith("*/"):
            if v % int(part[2:]) == 0:
                return True
        elif "-" in part:
            a, b = part.split("-")
            if int(a) <= v <= int(b):
                return True
        elif part.isdigit() and int(part) == v:
            return True
    return False


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="aaka Tools runner")
    ap.add_argument("name", nargs="?", help="tool to run (omit with --due)")
    ap.add_argument("--due", action="store_true", help="run all tools due now (cron)")
    ap.add_argument("--args", default="", help="extra args to pass the tool")
    a = ap.parse_args()
    if a.due:
        for nm, res in run_due():
            print(nm, "->", res.get("ok"), res.get("summary", "")[:80])
    elif a.name:
        print(json.dumps(run_and_report(a.name, a.args), indent=2))
    else:
        ap.print_help()
