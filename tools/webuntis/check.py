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


def _timetable(s, base: str, school: str, me: dict, date_arg: str, days: int = 1) -> int:
    import datetime as dt
    from collections import defaultdict
    if date_arg == "today":
        start = dt.date.today()
    elif date_arg == "tomorrow":
        start = dt.date.today() + dt.timedelta(days=1)
    else:
        start = dt.datetime.strptime(date_arg, "%Y%m%d").date()
    end = start + dt.timedelta(days=max(1, days) - 1)
    try:
        r = s.post(f"{base}/jsonrpc.do", params={"school": school}, timeout=20, json={
            "id": "aaka", "method": "getTimetable", "jsonrpc": "2.0", "params": {"options": {
                "element": {"id": me.get("personId"), "type": me.get("personType")},
                "startDate": int(start.strftime("%Y%m%d")), "endDate": int(end.strftime("%Y%m%d")),
                "showSubstText": True, "showLsText": True, "showInfo": True,
                "subjectFields": ["id", "name"], "roomFields": ["id", "name"]}}})
        body = r.json()
    except Exception as e:
        return _fail("network_error", f"timetable fetch failed: {e}")
    if body.get("error"):
        return _fail("api_error", f"getTimetable error: {body['error']}")
    periods = [p for p in (body.get("result") or []) if p.get("startTime")]
    rng = f"{start.strftime('%d.%m')}" + (f"–{end.strftime('%d.%m')}" if days > 1 else "")
    if not periods:
        return _ok(f"📅 No lessons {rng}.")
    subjects = _lookup(s, base, school, "getSubjects")
    rooms = _lookup(s, base, school, "getRooms")

    by_day = defaultdict(list)
    for p in periods:
        by_day[p.get("date")].append(p)

    out = []
    for ymd in sorted(by_day):
        dd = dt.datetime.strptime(str(ymd), "%Y%m%d").date()
        day = sorted(by_day[ymd], key=lambda p: p.get("startTime", 0))
        # reasons (Kennenlernfahrt, events…) surfaced from substText/lstext
        reasons = sorted({(p.get("substText") or p.get("lstext") or "").strip()
                          for p in day} - {""})
        rtxt = f" — {', '.join(reasons)}" if reasons else ""
        active = [p for p in day if p.get("code") != "cancelled"]
        head = f"*{dd.strftime('%a %d.%m')}*"
        if not active:
            out.append(f"{head}: ❌ all cancelled{rtxt}")
            continue
        out.append(head + (f"  _{', '.join(reasons)}_" if reasons else ""))
        for p in _merge_day(day, subjects, rooms):
            mark = " ❌" if p["code"] == "cancelled" else (" ⚠️" if p["code"] == "irregular" else "")
            rsn = f" — {p['reason']}" if p.get("reason") else ""
            out.append(f"  {p['t0']}–{p['t1']} {p['subj']}"
                       + (f" · {p['room']}" if p["room"] else "") + mark + rsn)
    summary = "📅 " + rng + "\n" + "\n".join(out)
    return _ok(summary, summary)


def _merge_day(day: list, subjects: dict, rooms: dict) -> list:
    """Resolve names + merge consecutive identical periods (doubles → one range)."""
    rows = []
    for p in day:
        rows.append({
            "t0": _hhmm(p["startTime"]), "t1": _hhmm(p["endTime"]),
            "subj": _map_names(p.get("su"), subjects) or "?",
            "room": _map_names(p.get("ro"), rooms),
            "code": p.get("code", ""),
            "reason": (p.get("substText") or p.get("lstext") or "").strip(),
        })
    merged = []
    for r in rows:
        if (merged and merged[-1]["subj"] == r["subj"] and merged[-1]["room"] == r["room"]
                and merged[-1]["code"] == r["code"] and merged[-1]["reason"] == r["reason"]):
            merged[-1]["t1"] = r["t1"]  # extend the block
        else:
            merged.append(dict(r))
    return merged


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
        return _timetable(s, base, school, me, args.date, args.days)

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
        payload = hw.json() or {}
        data = payload.get("data", {})
    except Exception as e:
        return _fail("api_error", f"homework fetch failed: {e}")

    # Cross-check (your "check against a count"): a valid response has a `data`
    # object with a `homeworks` list AND a `records` list that tracks homework
    # items. Distinguish "confirmed zero" from "couldn't read" — never silently
    # report "no homework" on a malformed/partial response.
    if not isinstance(data, dict) or "homeworks" not in data:
        return _fail("read_uncertain",
                     "⚠️ Couldn't read homework — WebUntis returned an unexpected "
                     "response (NOT reporting 'none'). Check the tool/endpoint.")
    homeworks = data.get("homeworks") or []
    records = data.get("records") or []
    if not homeworks and records:
        return _fail("read_uncertain",
                     f"⚠️ Homework read looks incomplete — {len(records)} lesson "
                     f"record(s) but 0 homework items parsed. Flagging, not 'none'.")

    lessons = {l.get("id"): l for l in (data.get("lessons", []) or [])}
    def subject_of(hwk):
        les = lessons.get(hwk.get("lessonId"), {})
        return les.get("subject") or les.get("name") or "?"

    open_hw = [h for h in homeworks if not h.get("completed")]
    open_hw.sort(key=lambda h: str(h.get("dueDate", "")))

    if not open_hw:
        n = len(homeworks)
        note = f" ({n} already completed)" if n else ""
        return _ok(f"📚 No open homework in the next {args.days} days{note}. (confirmed ✓)")

    lines = []
    for h in open_hw:
        due = str(h.get("dueDate", ""))
        due_fmt = f"{due[6:8]}.{due[4:6]}" if len(due) == 8 else due
        text = (h.get("text") or h.get("remark") or "").strip()
        lines.append(f"• {subject_of(h)} (due {due_fmt}): {text}")

    summary = f"📚 {len(open_hw)} open homework:\n" + "\n".join(lines[:8])
    return _ok(summary, "\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
