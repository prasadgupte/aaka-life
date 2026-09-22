# WebUntis homework — aaka Tool

Sensor-native WebUntis homework check (no browser). Auth via JSON-RPC → REST
token → homework endpoint. First reference tool for the aaka Tools framework
(`docs/aaka-tools.md`).

## Activate (2 steps)

**1. Drop credentials in the vault** (`/Users/Shared/secrets/`, outside git —
override the root with `AAKA_SECRETS_ROOT`, e.g. on the VPS):

```bash
mkdir -p /Users/Shared/secrets/webuntis
cat > /Users/Shared/secrets/webuntis/creds.json <<'JSON'
{ "server": "yourschool.webuntis.com", "school": "yourschool",
  "user": "<student-or-parent-login>", "password": "<password>" }
JSON
chmod 600 /Users/Shared/secrets/webuntis/creds.json
```

**2. Register the tool** — in a Claude session (`register_tool` MCP tool):

> register the webuntis homework tool: run
> /Users/Shared/aaka-repo/tools/webuntis/check.py, schedule "0 7 * * *",
> placement sensor, command /homework, report_to \<your-member-id\>,
> secrets webuntis, on_error alert

…or add to `$AAKA_CONFIG_DIR/config/tools.yaml`:

```yaml
homework:
  run: /Users/Shared/aaka-repo/tools/webuntis/check.py
  args: "--days 7"
  placement: sensor
  schedule: "0 7 * * *"
  command: "/homework"
  report_to: <your-member-id>
  on_error: alert
  secrets: webuntis
  enabled: true
```

## Test it

```bash
# direct (once creds are in place)
AAKA_TOOL_SECRETS=/Users/Shared/secrets/webuntis \
  venv/bin/python3 tools/webuntis/check.py --days 7

# via the framework (runs + reports through aaka)
venv/bin/python3 sensor/tool_runner.py homework
# or in chat:  /tools run homework
```

Expected: tomorrow's lessons, then exams, then homework — delivered to
`report_to`. On bad creds you get a `🔐 needs a re-auth` alert instead of silence.

## What the digest says

```
📅 23.09
*Wed 23.09*
  🕘 09:50–14:45 — starts 2 periods late     ← the day's shape, first
  09:50–11:20 E · 014
  ❌ 11:50–12:35 Gewi · 014 — Vertretung     ← status LEADS the line
  ...

📝 1 exam coming up (1 new ❗):              ← above homework: you prepare for it
❗ *Deutsch — Di 29.09, 08:00 (in 7 days)*

📚 3 open homework (1 new ❗):
❗ *Englisch (due 24.09): Copy the vocabulary …*
• Mathematik (due 25.09): LB S. 53 Nr. 10
```

- **Day verdict first.** All lessons cancelled → `🎉 NO SCHOOL`; some → `⚠️ N of M lessons
  cancelled`; an after-school club still running is listed under `still on:` (a club is not a
  lesson, so "no school" stays true and gets said).
- **Late start.** `🕘 <first>–<last>` always; `— starts N periods late` when lessons at the
  front of the day are missing from the plan (cancelled, or simply not this student's),
  measured against the school's own timegrid (`getTimegridUnits`).
- **Exams** come from the timetable the web app loads
  (`/api/public/timetable/weekly/data` → a period with `is.exam` + an `exam` object): the
  student account has no rights on `/api/exams` or JSON-RPC `getExams` (403 / "no right for
  getExams()"). One request per ISO week, 4 weeks ahead. `exams` is also a mode of its own.
- **❗ = new since the last run**, for exams and homework alike (24 h window, state in
  `$AAKA_CONFIG_DIR/data/webuntis/{hw,exam}_seen_<member>.json`). The very first run records
  its items as already-aged, so the run after a fresh install doesn't announce the whole
  backlog as new.

## Notes
- The tool prints one JSON line `{ok, summary, details, error}`; `auth_required`
  triggers aaka's re-auth prompt.
- `placement: sensor` = runs on the VPS every morning even if the Mac is off.
  Move to `executor` to run Mac-side.
- Generic to any WebUntis school — change `server`/`school` in `creds.json`.
