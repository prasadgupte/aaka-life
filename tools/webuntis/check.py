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


def _creds() -> dict:
    d = os.environ.get("AAKA_TOOL_SECRETS", "")
    path = os.path.join(d, "creds.json") if d else "creds.json"
    with open(path) as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="days ahead to check")
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
    # school context cookie some servers require for the REST API
    s.cookies.set("schoolname", '"_' + school + '"', domain=server)

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
