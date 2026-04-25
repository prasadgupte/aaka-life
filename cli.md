# Aaka CLI Reference

Copy-paste examples for every intent and dev operation.

---

## Setup

```bash
# Verify gateway abstraction loads
python3 -c "from gateway.adapter import GatewayAdapter; print('OK')"
python3 -c "from gateway.config import BACKEND; print('Backend:', BACKEND)"

# Verify config loads
AAKA_CONFIG_DIR=/Users/Shared/aaka-repo-config AAKA_CONTEXT=family \
  python3 aaka_config.py

# Build Docker image
docker compose build

# Test gateway import inside container
docker compose run --rm sensor \
  python3 -c "from gateway.adapter import GatewayAdapter; print('OK')"
```

---

## Sensor dry-run

```bash
# Zero-token intents
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/today"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/week"

# LLM extraction intents
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/add physio friday 3pm"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/addtask buy groceries by friday"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/engage"

# Buy lists
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "b rewe ?"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "b rewe add 1 3 5"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "b rewe #clear"

# Budget / expense tracking
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "x 45 groceries lidl"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "x"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "x undo"

# Undo queued action
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "undo #9a9a8ce0"

# Day schedule
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/day monday"

# Block time
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/block focus tomorrow 9-12"

# Fix calendar issues
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "c fix"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "c fix 3 Alex"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "c fix 3 #work"

# Task shortcut (t)
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "t"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "t call dentist by friday"

# Task management
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/done 1"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/done 1 3 5"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/done today"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/tasks overdue"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/edit 2 friday"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/del 3"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/snooze 1 3d"

docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/snooze all 1d"

# List tags
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/tags"

# Tag management
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/tag list"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/tag ortho"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/tag jp alias 2607-japan-china"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/tag ortho route health/ortho"

# Notes — write (routed topics go to _context.md in vault)
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "n ortho first visit"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "n ortho !ari-ortho-treatment first visit"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "n jp bought rail pass"

# Notes — read (aliases resolved automatically)
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "n ortho"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "n jp"

# Drop file
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/drop invoice"

# Drop help
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "f help"
```

---

## Queue worker (Mac-native)

```bash
# Run once (process all confirmed items)
QUEUE_DB=/Users/Shared/aaka-repo-config/family/data/queue/butler.db \
  python3 executor/queue_worker.py --once

# Inspect queue DB
sqlite3 /Users/Shared/aaka-repo-config/family/data/queue/butler.db \
  "SELECT id, intent, status, created_at FROM queue_items ORDER BY created_at DESC LIMIT 20;"
```

---

## Message send

```bash
# Send via configured gateway
python3 message_send.py "Test message"
python3 message_send.py --channel telegram --to 123456789 "Alert!"
python3 message_send.py --to family-group "Dinner at 7"
python3 message_send.py --dry-run "Test dry run"
```

---

## Gateway backend switch

```bash
# Use ZeroClaw (default)
export GATEWAY_BACKEND=zeroclaw
python3 -c "from gateway.config import BACKEND; print(BACKEND)"

# Fall back to OpenClaw
export GATEWAY_BACKEND=openclaw
python3 -c "from gateway.config import BACKEND; print(BACKEND)"
```

---

## Calendar sync (Mac-native)

```bash
# Manual sync
AAKA_CONFIG_DIR=/Users/Shared/aaka-repo-config python3 skills/calendar/sidecar_sync.py
```

---

## Docker compose

```bash
# Start sensor (dev)
docker compose up sensor

# View logs
docker compose logs -f sensor

# Rebuild after code change
docker compose build sensor && docker compose up sensor

# Prod VPS
docker compose -f docker-compose.prod.yml up -d sensor
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs sensor
```

---

## Password Store

```bash
# One-time setup (stores master password in macOS Keychain)
python3 tools/pw_setup.py

# Import LastPass CSV export
python3 tools/pw_import.py /path/to/lastpass_export.csv --dry-run  # preview
python3 tools/pw_import.py /path/to/lastpass_export.csv            # import
python3 tools/pw_import.py /path/to/lastpass_export.csv --overwrite # update dupes

# Add a single entry (e.g. a mail account password)
python3 -c "
from tools.password_store import PasswordStore
PasswordStore().add_entry('email/personal-pop', 'user@example.com', 'mypassword',
    url='pop.example.com', grouping='email')
"

# API usage (in other skills/agents)
from tools.password_store import PasswordStore
store = PasswordStore()
store.search("gmail")          # list matching entries (no passwords)
store.get_password("email/personal-pop")  # returns password string
```

---

## Mail Fetch

```bash
# One-time: set up password store and accounts config
python3 tools/pw_setup.py
python3 tools/pw_import.py /path/to/lastpass_export.csv
# Edit /Users/Shared/secrets/mail-fetch/accounts.yaml with your accounts

# Run fetch manually
python3 skills/mail/cron_fetch.py

# Install launchd agent (runs every 15 min)
export AAKA_BASE=/Users/Shared/aaka-repo
sed "s|\${AAKA_BASE}|$AAKA_BASE|g" executor/com.aaka.mailfetch.plist \
  > ~/Library/LaunchAgents/com.aaka.mailfetch.plist
launchctl load ~/Library/LaunchAgents/com.aaka.mailfetch.plist
launchctl list com.aaka.mailfetch   # verify (PID should be non-zero)

# Check mail data
ls /Users/Shared/aaka-repo-config/data/mail/
cat /Users/Shared/aaka-repo-config/data/mail/<account>/.last_fetch.json
```

---

## PDF tool

```bash
# CLI — all 5 operations
python3 tools/pdf_tool.py compress input.pdf -o out.pdf -q 75
python3 tools/pdf_tool.py extract  input.pdf -o ./extracted/
python3 tools/pdf_tool.py split    input.pdf "1,5,9" -o ./parts/
python3 tools/pdf_tool.py split    input.pdf "2s"    -o ./parts/
python3 tools/pdf_tool.py merge    a.pdf b.pdf photo.jpg -o combined.pdf
python3 tools/pdf_tool.py delete   input.pdf "3-5"  -o trimmed.pdf
python3 tools/pdf_tool.py delete-blank-pages input.pdf                  # auto-remove blanks, writes input_trimmed.pdf + input_blanks.pdf
python3 tools/pdf_tool.py delete-blank-pages input.pdf --dont-return    # skip the *_blanks.pdf verification file
python3 tools/pdf_tool.py ocr input.pdf                                 # OCR via Tesseract → input_text.txt
python3 tools/pdf_tool.py ocr input.pdf --language eng+deu              # multi-language OCR
python3 tools/pdf_tool.py help

# Page spec self-test
python3 tools/pdf_page_spec.py

# Sensor dry-run (tests intent routing)
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/pdf help"

# mail_view dry-run
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/mail"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "m personal-pop 5"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "m read personal-pop 1"

# pw_manage dry-run
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/pw list"
docker compose run --rm sensor \
  python3 sensor/router_sensor.py --dry-run "/pw search gmail"

# Agent API (gateway must be running)
curl http://localhost:18790/health
python3 -c "
import base64, json, urllib.request
pdf = base64.b64encode(open('input.pdf','rb').read()).decode()
body = json.dumps({'file_b64': pdf, 'filename': 'input.pdf'}).encode()
req = urllib.request.Request(
    'http://localhost:18790/v1/pdf/compress',
    data=body,
    headers={'Content-Type':'application/json','X-Agent-Key':'YOUR_KEY'},
    method='POST',
)
with urllib.request.urlopen(req) as r:
    print(json.loads(r.read()))
"
```
