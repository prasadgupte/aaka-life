#!/usr/bin/env python3
"""
tools/webuntis/check.py — aaka Tool: check WebUntis homework (sensor-native, no browser).

WebUntis (used by many EU schools) has an HTTP API despite its SPA front-end:
  1. JSON-RPC authenticate → JSESSIONID session cookie
  2. GET /WebUntis/api/token/new → a bearer JWT
  3. GET /WebUntis/api/homeworks/lessons?startDate&endDate → homework list

Run contract (aaka Tools): reads creds from $AAKA_TOOL_SECRETS/creds.json, prints
a single structured JSON result on stdout, exits 0 on ok / non-zero on error.
  {"ok": true, "summary": "...", "details": "...", "error": null}
On bad credentials it returns error="auth_required" so aaka can prompt a re-auth.

creds.json (in the vault, outside git):
  {"server": "yourschool.webuntis.com", "school": "yourschool",
   "user": "<login>", "password": "<password>"}

Usage:  AAKA_TOOL_SECRETS=/path/to/secrets/webuntis python3 check.py [--days 7]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import requests


def _fail(error: str, summary: str, code: int = 1) -> int:
    print(json.dumps({"ok": False, "summary": summary, "details": "", "error": error}))
    return code


def _ok(summary: str, details: str = "") -> int:
    print(json.dumps({"ok": True, "summary": summary, "details": details, "error": None}))
    return 0


def _hhmm(t) -> str:
    t = int(t)
    return f"{t // 100:02d}:{t % 100:02d}"


def _names(lst) -> str:
    return ", ".join(x.get("name", "?") for x in (lst or []) if isinstance(x, dict))


def _lookup(s, base: str, school: str, method: str) -> dict:
    """Fetch WebUntis master data (getSubjects/getRooms/...) → {id: name}."""
    try:
        r = s.post(f"{base}/jsonrpc.do", params={"school": school}, timeout=15,
                   json={"id": "aaka", "method": method, "jsonrpc": "2.0", "params": {}})
        return {x["id"]: (x.get("name") or x.get("longName") or "?")
                for x in (r.json().get("result") or []) if x.get("id") is not None}
    except Exception:
        return {}


def _map_names(lst, m) -> str:
    """Map [{id}] → 'name, name' using a lookup dict (falls back to inline name)."""
    out = []
    for x in (lst or []):
        if not isinstance(x, dict):
            continue
        out.append(m.get(x.get("id")) or x.get("name") or "?")
    return ", ".join(out)


def _timetable(s, base: str, school: str, me: dict, date_arg: str) -> int:
    import datetime as dt
    if date_arg == "today":
        d = dt.date.today()
    elif date_arg == "tomorrow":
        d = dt.date.today() + dt.timedelta(days=1)
    else:
        d = dt.datetime.strptime(date_arg, "%Y%m%d").date()
    ymd = int(d.strftime("%Y%m%d"))
    try:
        r = s.post(f"{base}/jsonrpc.do", params={"school": school}, timeout=15, json={
            "id": "aaka", "method": "getTimetable", "jsonrpc": "2.0",
            "params": {"id": me.get("personId"), "type": me.get("personType"),
                       "startDate": ymd, "endDate": ymd}})
        body = r.json()
    except Exception as e:
        return _fail("network_error", f"timetable fetch failed: {e}")
    if body.get("error"):
        return _fail("api_error", f"getTimetable error: {body['error']}")
    periods = [p for p in (body.get("result") or []) if p.get("startTime")]
    periods.sort(key=lambda p: p.get("startTime", 0))
    label = d.strftime("%a %d.%m")
    if not periods:
        return _ok(f"📅 No lessons on {label}.")
    # su/ro come back as [{id}] — resolve names from master data.
    subjects = _lookup(s, base, school, "getSubjects")
    rooms = _lookup(s, base, school, "getRooms")
    lines = []
    for p in periods:
        code = p.get("code", "")  # "cancelled" | "irregular" | ""
        mark = " ❌ cancelled" if code == "cancelled" else (" ⚠️ changed" if code == "irregular" else "")
        subj = _map_names(p.get("su"), subjects) or "?"
        room = _map_names(p.get("ro"), rooms)
        lines.append(f"{_hhmm(p['startTime'])}–{_hhmm(p['endTime'])} {subj}"
                     + (f" · {room}" if room else "") + mark)
    active = len([p for p in periods if p.get("code") != "cancelled"])
    return _ok(f"📅 {label} — {active} lessons:\n" + "\n".join(lines), "\n".join(lines))


def _creds() -> dict:
    d = os.environ.get("AAKA_TOOL_SECRETS", "")
    path = os.path.join(d, "creds.json") if d else "creds.json"
    with open(path) as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["homework", "timetable"], default="homework")
    ap.add_argument("--days", type=int, default=7, help="days ahead (homework mode)")
    ap.add_argument("--date", default="tomorrow", help="timetable date: today|tomorrow|YYYYMMDD")
    args = ap.parse_args()

    try:
        c = _creds()
    except Exception as e:
        return _fail("config_error", f"can't read creds.json: {e}")

    server = c["server"].replace("https://", "").rstrip("/")
    school = c["school"]
    base = f"https://{server}/WebUntis"
    s = requests.Session()
    s.headers["User-Agent"] = "aaka-tool/webuntis"

    # 1. authenticate (JSON-RPC) → session cookie
    try:
        r = s.post(f"{base}/jsonrpc.do", params={"school": school}, timeout=15, json={
            "id": "aaka", "method": "authenticate", "jsonrpc": "2.0",
            "params": {"user": c["user"], "password": c["password"], "client": "aaka"},
        })
        body = r.json()
    except Exception as e:
        return _fail("network_error", f"WebUntis unreachable: {e}")
    if body.get("error"):
        code = body["error"].get("code")
        # -8504 bad credentials, -8998 too many attempts, -8509 locked
        if code in (-8504, -8509, -8998):
            return _fail("auth_required", f"WebUntis login failed: {body['error'].get('message', code)}")
        return _fail("api_error", f"authenticate error: {body['error']}")
    me = body.get("result", {}) or {}
    # school context cookie some servers require for the REST API
    s.cookies.set("schoolname", '"_' + school + '"', domain=server)

    if args.mode == "timetable":
        return _timetable(s, base, school, me, args.date)

    # 2. bearer token for the REST API
    try:
        tok = s.get(f"{base}/api/token/new", timeout=15)
        if tok.status_code != 200 or not tok.text.strip():
            return _fail("auth_required", "couldn't get an API token (session not accepted)")
        bearer = tok.text.strip()
    except Exception as e:
        return _fail("network_error", f"token fetch failed: {e}")

    # 3. homework for the window
    today = dt.date.today()
    end = today + dt.timedelta(days=max(1, args.days))
    try:
        hw = s.get(f"{base}/api/homeworks/lessons", timeout=15,
                   headers={"Authorization": f"Bearer {bearer}"},
                   params={"startDate": today.strftime("%Y%m%d"),
                           "endDate": end.strftime("%Y%m%d")})
        data = (hw.json() or {}).get("data", {})
    except Exception as e:
        return _fail("api_error", f"homework fetch failed: {e}")

    homeworks = data.get("homeworks", []) or []
    lessons = {l.get("id"): l for l in (data.get("lessons", []) or [])}
    # subject lookup: lesson → subject name
    def subject_of(hwk):
        les = lessons.get(hwk.get("lessonId"), {})
        return les.get("subject") or les.get("name") or "?"

    open_hw = [h for h in homeworks if not h.get("completed")]
    open_hw.sort(key=lambda h: str(h.get("dueDate", "")))

    if not open_hw:
        print(json.dumps({"ok": True, "summary": "📚 No open homework in the next "
                          f"{args.days} days.", "details": "", "error": None}))
        return 0

    lines = []
    for h in open_hw:
        due = str(h.get("dueDate", ""))
        due_fmt = f"{due[6:8]}.{due[4:6]}" if len(due) == 8 else due
        text = (h.get("text") or h.get("remark") or "").strip()
        lines.append(f"• {subject_of(h)} (due {due_fmt}): {text}")

    summary = f"📚 {len(open_hw)} open homework:\n" + "\n".join(lines[:8])
    print(json.dumps({"ok": True, "summary": summary,
                      "details": "\n".join(lines), "error": None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
