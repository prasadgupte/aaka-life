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
import re
import sys

import requests


def _R(ok: bool, summary: str, details: str = "", error=None) -> dict:
    return {"ok": ok, "summary": summary, "details": details, "error": error}


def _fail(error: str, summary: str) -> dict:
    return _R(False, summary, "", error)


def _ok(summary: str, details: str = "") -> dict:
    return _R(True, summary, details)


def _emit(d: dict) -> int:
    """Print one result line + return an exit code (0 ok / 1 error)."""
    print(json.dumps(d))
    return 0 if d.get("ok") else 1


# ── "what's new" marking ──────────────────────────────────────────────────────
# An assignment first seen within HW_NEW_HOURS is prefixed ❗ and bolded, so the
# evening message shows what came in since yesterday instead of the same list
# again. State: one small JSON per member in $AAKA_CONFIG_DIR/data/webuntis/
# ({homework key: first-seen ISO}). The very first run only records — nothing is
# "new" when there is no earlier run to compare against.
HW_NEW_HOURS = 24
HW_SEEN_KEEP_DAYS = 30


def _hw_key(h: dict) -> str:
    """WebUntis' own homework id; falls back to lesson+due+text if it is missing."""
    return str(h.get("id") or f"{h.get('lessonId')}|{h.get('dueDate')}|{(h.get('text') or '')[:40]}")


def _seen_path(member: str, kind: str = "hw") -> str:
    cfg = os.environ.get("AAKA_CONFIG_DIR", "")
    if not cfg:
        return ""
    slug = re.sub(r"[^A-Za-z0-9_-]", "", member or "")
    return os.path.join(cfg, "data", "webuntis", f"{kind}_seen{('_' + slug) if slug else ''}.json")


def _hw_seen_path(member: str) -> str:
    return _seen_path(member, "hw")


def _mark_new_keys(keys: set, path: str, hours: int, now: "dt.datetime | None" = None) -> set:
    """Which of `keys` were first seen within `hours`, against the JSON store at
    `path` ({key: first-seen ISO}). Updates the store.

    The very first run has nothing to compare against, so it records its items
    as already-aged and marks none: otherwise the *next* run would announce the
    entire backlog as "new" (the bootstrap items are all < `hours` old)."""
    if not path:
        return set()
    now = now or dt.datetime.now(dt.timezone.utc)
    first_run = not os.path.exists(path)
    try:
        with open(path) as f:
            seen = json.load(f)
        if not isinstance(seen, dict):
            seen = {}
    except Exception:
        seen = {}
    stamp = (now - dt.timedelta(hours=hours + 1) if first_run else now).isoformat(timespec="seconds")
    new = set()
    for k in keys:
        if k not in seen:
            seen[k] = stamp
        if first_run:
            continue
        try:
            first = dt.datetime.fromisoformat(seen[k])
        except (TypeError, ValueError):
            continue
        if first.tzinfo is None:
            first = first.replace(tzinfo=dt.timezone.utc)
        if (now - first) <= dt.timedelta(hours=hours):
            new.add(k)
    cutoff = now - dt.timedelta(days=HW_SEEN_KEEP_DAYS)

    def _keep(k: str, v: str) -> bool:
        if k in keys:
            return True
        try:
            ts = dt.datetime.fromisoformat(v)
            return (ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)) >= cutoff
        except (TypeError, ValueError):
            return False
    seen = {k: v for k, v in seen.items() if _keep(k, v)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(seen, f)
        os.replace(tmp, path)
    except OSError:
        pass  # marking is best-effort; never fail the read over it
    return new


def _mark_new(open_hw: list, member: str = "", now: "dt.datetime | None" = None) -> set:
    """Keys of the open assignments first seen within HW_NEW_HOURS."""
    return _mark_new_keys({_hw_key(h) for h in open_hw}, _hw_seen_path(member), HW_NEW_HOURS, now)


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


# Optional/after-school items. They are NOT lessons, so "all lessons cancelled"
# stays true (and gets said) even when a club still runs.
_ACTIVITY_PREFIXES = ("AG-", "AG ", "Schulclub", "Ganztag", "Betreuung", "Mittag")


def _is_activity(subj: str) -> bool:
    x = (subj or "").strip()
    return any(x.startswith(pfx) for pfx in _ACTIVITY_PREFIXES)


def _timegrid_starts(s, base: str, school: str) -> dict:
    """{weekday(1=Sun…7=Sat per WebUntis): [slot start ints]} — what a normal
    school day looks like, so a late start can be named as one."""
    try:
        r = s.post(f"{base}/jsonrpc.do", params={"school": school}, timeout=15,
                   json={"id": "aaka", "method": "getTimegridUnits", "jsonrpc": "2.0", "params": {}})
        out = {}
        for d in (r.json().get("result") or []):
            units = sorted(int(u["startTime"]) for u in (d.get("timeUnits") or []) if u.get("startTime"))
            if units:
                out[d.get("day")] = units
        return out
    except Exception:
        return {}


def _slots_before(starts: list, first: int) -> int:
    """How many normal slots are skipped before `first`."""
    return sum(1 for t in (starts or []) if t < first)


def _day_start_line(rows: list, starts: list) -> str:
    """The one fact a parent reads first: when does school actually start today?
    Named as a late start only when lessons before it are missing from the plan
    (cancelled, or simply not his) — otherwise the times below say it already."""
    live = [r for r in rows if r["code"] != "cancelled" and not _is_activity(r["subj"])]
    if not live:
        return ""
    first = int(live[0]["t0"].replace(":", ""))
    skipped = _slots_before(starts, first)
    end = max(int(r["t1"].replace(":", "")) for r in live)
    line = f"🕘 {live[0]['t0']}–{end // 100:02d}:{end % 100:02d}"
    if skipped:
        line += f" — starts {skipped} period{'s' if skipped > 1 else ''} late"
    return line


def _format_day(rows: list, head: str, reasons: list, rtxt: str, starts: list) -> list:
    """Render one day. Pure — no I/O — so every branch is unit-testable.

    The day-level verdict comes FIRST: a trailing ❌ per line is easy to skim
    past, and "you start at 10:35" / "every lesson is cancelled" is what a
    parent actually needs. Per-line status then LEADS the line."""
    lessons = [r for r in rows if not _is_activity(r["subj"])]
    acts = [r for r in rows if _is_activity(r["subj"])]
    dead = [r for r in lessons if r["code"] == "cancelled"]
    live_acts = [r for r in acts if r["code"] != "cancelled"]
    out = []

    if lessons and len(dead) == len(lessons):
        if not live_acts:
            return [f"{head}: 🎉 NO SCHOOL — all {len(lessons)} lessons cancelled{rtxt}"]
        out.append(f"{head}: 🚫 ALL {len(lessons)} LESSONS CANCELLED{rtxt}")
        out.append("  still on: " + ", ".join(f"{r['t0']}–{r['t1']} {r['subj']}" for r in live_acts))
        return out

    rsns = f"  _{', '.join(reasons)}_" if reasons else ""
    if dead:
        out.append(f"{head} — ⚠️ {len(dead)} of {len(lessons)} lessons cancelled{rsns}")
    else:
        out.append(head + rsns)
    start_line = _day_start_line(rows, starts)
    if start_line:
        out.append("  " + start_line)
    for r in rows:
        mark = "❌ " if r["code"] == "cancelled" else ("⚠️ " if r["code"] == "irregular" else "")
        rsn = f" — {r['reason']}" if r.get("reason") else ""
        out.append(f"  {mark}{r['t0']}–{r['t1']} {r['subj']}"
                   + (f" · {r['room']}" if r["room"] else "") + rsn)
    return out


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

    grid = _timegrid_starts(s, base, school)
    out = []
    for ymd in sorted(by_day):
        dd = dt.datetime.strptime(str(ymd), "%Y%m%d").date()
        day = sorted(by_day[ymd], key=lambda p: p.get("startTime", 0))
        # reasons (Kennenlernfahrt, events…) surfaced from substText/lstext
        reasons = sorted({(p.get("substText") or p.get("lstext") or "").strip()
                          for p in day} - {""})
        rtxt = f" — {', '.join(reasons)}" if reasons else ""
        head = f"*{dd.strftime('%a %d.%m')}*"
        # WebUntis weekdays are 1=Sun … 7=Sat; python's weekday() is 0=Mon.
        starts = grid.get((dd.weekday() + 2) % 7 or 7) or (sorted(grid.values())[0] if grid else [])
        out.extend(_format_day(_merge_day(day, subjects, rooms), head, reasons, rtxt, starts))
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


def _creds(member: str = "") -> dict:
    """Read creds: per-member <secrets>/<member>/creds.json, else <secrets>/creds.json.

    `member` is untrusted (comes from a chat command), so it's restricted to a
    plain slug — no path separators or traversal (`../`) that could read a
    creds.json outside the tool's secrets dir."""
    d = os.environ.get("AAKA_TOOL_SECRETS", "")
    if member and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", member):
        raise ValueError(f"invalid member id: {member!r}")
    candidates = []
    if member and d:
        candidates.append(os.path.join(d, member, "creds.json"))
    candidates.append(os.path.join(d, "creds.json") if d else "creds.json")
    for p in candidates:
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    raise FileNotFoundError(candidates[0])


# ── Exams ─────────────────────────────────────────────────────────────────────
# The student account has no rights on /api/exams or JSON-RPC getExams (403 /
# "no right for getExams()"), but the timetable the web app itself loads carries
# them: a period with is.exam = true and an `exam` object {name, id, date}.
# So exams are read from the weekly timetable, one request per ISO week.
EXAM_FORWARD_DAYS = 28
EXAM_NEW_HOURS = 24
_WEEKDAY_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def _exam_key(e: dict) -> str:
    return str(e.get("id") or f"{e.get('date')}|{e.get('subject')}")


def _week_periods(s, base: str, me: dict, day: "dt.date") -> list:
    """Periods of the ISO week containing `day`, from the endpoint the web app
    uses. Returns [] on any error — exams must never break the digest."""
    try:
        r = s.get(f"{base}/api/public/timetable/weekly/data", timeout=20,
                  params={"elementType": me.get("personType"), "elementId": me.get("personId"),
                          "date": day.isoformat(), "formatId": 1})
        data = (r.json().get("data") or {}).get("result", {}).get("data", {})
        return data.get("elementPeriods", {}).get(str(me.get("personId")), []) or []
    except Exception:
        return []


def _exams(s, base: str, me: dict, member: str = "", days: int = EXAM_FORWARD_DAYS) -> dict:
    """Upcoming exams in the next `days`, newest-first-seen marked ❗.
    Returns {"ok", "summary", "count"} — never raises, never fails the digest."""
    today = dt.date.today()
    horizon = today + dt.timedelta(days=days)
    found: dict = {}
    day = today
    while day <= horizon:                      # one call per ISO week
        for p in _week_periods(s, base, me, day):
            if not (p.get("is") or {}).get("exam"):
                continue
            ex = p.get("exam") or {}
            try:
                d = dt.datetime.strptime(str(p.get("date")), "%Y%m%d").date()
            except ValueError:
                continue
            if not (today <= d <= horizon):
                continue
            entry = {"id": ex.get("id"), "date": d, "start": int(p.get("startTime") or 0),
                     "subject": (ex.get("name") or "").strip() or "?"}
            key = _exam_key(entry)          # one identity, in-run and in the seen-store
            prev = found.get(key)
            if prev is None or entry["start"] < prev["start"]:
                found[key] = entry
        day += dt.timedelta(days=7)
    if not found:
        return {"ok": True, "summary": "", "count": 0}
    exams = sorted(found.values(), key=lambda e: (e["date"], e["start"]))
    new = _mark_new_keys({_exam_key(e) for e in exams},
                         _seen_path(member, "exam"), EXAM_NEW_HOURS)
    lines = []
    for e in exams:
        when = f"{_WEEKDAY_DE[e['date'].weekday()]} {e['date'].strftime('%d.%m')}"
        at = f", {_hhmm(e['start'])}" if e["start"] else ""
        left = (e["date"] - today).days
        in_days = "today" if left == 0 else ("tomorrow" if left == 1 else f"in {left} days")
        line = f"{e['subject']} — {when}{at} ({in_days})"
        lines.append(f"❗ *{line.replace('*', '∗')}*" if _exam_key(e) in new else f"• {line}")
    n_new = f" ({len(new)} new ❗)" if new else ""
    head = f"📝 {len(exams)} exam{'s' if len(exams) != 1 else ''} coming up{n_new}:"
    return {"ok": True, "summary": head + "\n" + "\n".join(lines[:6]), "count": len(exams)}


def _homework(s, base: str, school: str, me: dict, days: int, member: str = "") -> dict:
    """Fetch + cross-check homework (never a silent false 'none'). Returns a dict.
    Assignments first seen in the last HW_NEW_HOURS are marked ❗ + bold."""
    try:
        tok = s.get(f"{base}/api/token/new", timeout=15)
        if tok.status_code != 200 or not tok.text.strip():
            return _fail("auth_required", "couldn't get an API token (session not accepted)")
        bearer = tok.text.strip()
    except Exception as e:
        return _fail("network_error", f"token fetch failed: {e}")
    today = dt.date.today()
    end = today + dt.timedelta(days=max(1, days))
    try:
        hw = s.get(f"{base}/api/homeworks/lessons", timeout=15,
                   headers={"Authorization": f"Bearer {bearer}"},
                   params={"startDate": today.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")})
        data = (hw.json() or {}).get("data", {})
    except Exception as e:
        return _fail("api_error", f"homework fetch failed: {e}")
    if not isinstance(data, dict) or "homeworks" not in data:
        return _fail("read_uncertain",
                     "⚠️ Couldn't read homework — unexpected WebUntis response (NOT reporting 'none').")
    homeworks = data.get("homeworks") or []
    records = data.get("records") or []
    if not homeworks and records:
        return _fail("read_uncertain",
                     f"⚠️ Homework read incomplete — {len(records)} records but 0 items parsed.")
    lessons = {l.get("id"): l for l in (data.get("lessons", []) or [])}
    def subject_of(h):
        les = lessons.get(h.get("lessonId"), {})
        return les.get("subject") or les.get("name") or "?"
    open_hw = sorted([h for h in homeworks if not h.get("completed")],
                     key=lambda h: str(h.get("dueDate", "")))
    if not open_hw:
        n = len(homeworks)
        note = f" ({n} completed)" if n else ""
        return _ok(f"📚 No open homework in the next {days} days{note}. (confirmed ✓)")
    new_keys = _mark_new(open_hw, member)
    lines = []
    for h in open_hw:
        due = str(h.get("dueDate", ""))
        due_fmt = f"{due[6:8]}.{due[4:6]}" if len(due) == 8 else due
        text = (h.get("text") or h.get("remark") or "").strip()
        line = f"{subject_of(h)} (due {due_fmt}): {text}"
        if _hw_key(h) in new_keys:
            lines.append(f"❗ *{line.replace('*', '∗')}*")   # new since the last run → bold + bang
        else:
            lines.append(f"• {line}")
    n_new = f" ({len(new_keys)} new ❗)" if new_keys else ""
    return _ok(f"📚 {len(open_hw)} open homework{n_new}:\n" + "\n".join(lines[:8]), "\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tokens", nargs="*", help="e.g. 'kid1 digest' — member + mode")
    ap.add_argument("--member", default="")
    ap.add_argument("--mode", default="")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--date", default="tomorrow")
    args = ap.parse_args()

    modes = {"homework", "timetable", "digest", "exams"}
    if any(t.lower() in ("help", "-h", "--help", "?") for t in args.tokens):
        return _emit(_ok(
            "🏫 *WebUntis* — school timetable & homework.\n"
            "Usage: `/tools/webuntis <kid> <what>`\n"
            "• `digest` — tomorrow's lessons + exams + homework (default)\n"
            "• `exams` — written tests in the next 4 weeks\n"
            "• `homework` — open homework (next 7 days)\n"
            "• `timetable` — tomorrow's lessons (add `week` for 7 days)\n"
            "`<kid>` picks whose school login to use (per-member creds).\n"
            "e.g. `/tools/webuntis <kid> homework`"))

    member, mode = args.member, args.mode
    for t in args.tokens:  # positional 'kid1 digest' → member + mode
        low = t.lower()
        if low in modes:
            mode = low
        elif low == "week":
            args.days = 7
        elif not t.lstrip("-").isdigit():
            member = member or t
    mode = mode or "digest"

    try:
        c = _creds(member)
    except Exception as e:
        return _emit(_fail("config_error", f"can't read creds{(' for ' + member) if member else ''}: {e}"))

    server = c["server"].replace("https://", "").rstrip("/")
    school = c["school"]
    base = f"https://{server}/WebUntis"
    s = requests.Session()
    s.headers["User-Agent"] = "aaka-tool/webuntis"

    try:
        body = s.post(f"{base}/jsonrpc.do", params={"school": school}, timeout=15, json={
            "id": "aaka", "method": "authenticate", "jsonrpc": "2.0",
            "params": {"user": c["user"], "password": c["password"], "client": "aaka"}}).json()
    except Exception as e:
        return _emit(_fail("network_error", f"WebUntis unreachable: {e}"))
    if body.get("error"):
        code = body["error"].get("code")
        if code in (-8504, -8509, -8998):  # bad creds / locked / too many attempts
            return _emit(_fail("auth_required", f"WebUntis login failed: {body['error'].get('message', code)}"))
        return _emit(_fail("api_error", f"authenticate error: {body['error']}"))
    me = body.get("result", {}) or {}
    s.cookies.set("schoolname", '"_' + school + '"', domain=server)

    if mode == "timetable":
        r = _timetable(s, base, school, me, args.date, args.days)
    elif mode == "homework":
        r = _homework(s, base, school, me, args.days, member)
    elif mode == "exams":
        r = _exams(s, base, me, member, args.days if args.days > 7 else EXAM_FORWARD_DAYS)
        if not r["count"]:
            r = _ok(f"📝 No exams in the next {EXAM_FORWARD_DAYS} days.")
        else:
            r = _ok(r["summary"], r["summary"])
    else:  # digest = tomorrow's timetable + exams + homework, in one message
        tt = _timetable(s, base, school, me, "tomorrow", 1)
        ex = _exams(s, base, me, member)          # above homework: a date you prepare for
        hw = _homework(s, base, school, me, args.days, member)
        parts = [x["summary"] if x["ok"] else f"⚠️ {x['error']}: {x['summary']}" for x in (tt, hw)]
        if ex["count"]:
            parts.insert(1, ex["summary"])
        err = None if (tt["ok"] and hw["ok"]) else (tt.get("error") or hw.get("error"))
        r = _R(tt["ok"] or hw["ok"], "\n\n".join(parts), "\n\n".join(parts), err)
    return _emit(r)


if __name__ == "__main__":
    sys.exit(main())
