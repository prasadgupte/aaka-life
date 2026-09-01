# WebUntis homework — aaka Tool

Sensor-native WebUntis homework check (no browser). Auth via JSON-RPC → REST
token → homework endpoint. First reference tool for the aaka Tools framework
(`docs/aaka-tools.md`).

## Activate (2 steps)

**1. Drop credentials in the vault** (outside git). On the machine that will run
it (sensor for always-on, or the Mac):

```bash
mkdir -p "$AAKA_CONFIG_DIR/secrets/webuntis"
cat > "$AAKA_CONFIG_DIR/secrets/webuntis/creds.json" <<'JSON'
{ "server": "yourschool.webuntis.com", "school": "yourschool",
  "user": "<student-or-parent-login>", "password": "<password>" }
JSON
chmod 600 "$AAKA_CONFIG_DIR/secrets/webuntis/creds.json"
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
AAKA_TOOL_SECRETS="$AAKA_CONFIG_DIR/secrets/webuntis" \
  venv/bin/python3 tools/webuntis/check.py --days 7

# via the framework (runs + reports through aaka)
venv/bin/python3 sensor/tool_runner.py homework
# or in chat:  /tools run homework
```

Expected: `📚 N open homework: • Subject (due dd.mm): text …` — delivered to
`report_to`. On bad creds you get a `🔐 needs a re-auth` alert instead of silence.

## Notes
- The tool prints one JSON line `{ok, summary, details, error}`; `auth_required`
  triggers aaka's re-auth prompt.
- `placement: sensor` = runs on the VPS every morning even if the Mac is off.
  Move to `executor` to run Mac-side.
- Generic to any WebUntis school — change `server`/`school` in `creds.json`.
