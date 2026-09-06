# Installing Aaka with Claude

This guide is designed for Claude Code. Each phase is tagged:
- **[Claude]** — Claude can run this autonomously via tool calls
- **[Human]** — Requires a browser, phone, or paste action from you

Claude: read this file fully, then collect the required credentials below before touching any files.

**If you're reading this in Claude Code:** open `CLAUDE_SETUP.md` instead — it has a guided install experience with tier-by-tier milestones and handles partial setups gracefully.

---

## Local-first install (recommended starting point)

You don't need a VPS to start. Phases 1–4 run entirely on your Mac. The bot works whenever your Mac is on. Add the VPS (Phase 5+) later when you want always-on.

**Minimum to get a working bot:**
1. Telegram bot token + your user ID (from @BotFather and @userinfobot)
2. Python 3.11+ and git (**no Docker** — the sensor runs natively)
3. About 10 minutes

**Config directory:** Aaka stores runtime data in `~/aaka/config` by default (code lives in
`~/aaka/code`). Set `AAKA_CONFIG_DIR` to a different path if you prefer.

---

## Before you start — collect these

Ask the user for each item before beginning Phase 1.

| Item | How to get it | Required for |
|------|--------------|-------------|
| Telegram Bot Token | Open Telegram → @BotFather → `/newbot` → copy the `123456:ABC...` token | Bot messaging (required) |
| Telegram User ID | Message @userinfobot → copy the numeric `Id` field | DM routing (required) |
| Telegram Group ID | Add @RawDataBot to your group → it replies with the numeric group ID (negative number) | Group routing (optional) |
| Gemini API key | aistudio.google.com → Get API key (free tier) | Natural language (optional, Tier 2) |
| VPS IP address | Your hosting provider's control panel | Always-on (optional, Tier 3) |
| Family members | Name, Telegram ID, role (`admin`/`member`), short namespace | aaka.yaml |
| Calendar ID(s) | Google Calendar → Settings → click calendar name → "Calendar ID" | Calendar reads (optional, Tier 1) |
| WhatsApp phone | Your number in E.164, e.g. `491700000000@s.whatsapp.net` (optional) | WhatsApp channel |
| WhatsApp group JID | From openclaw logs after first WA link, e.g. `120363...@g.us` (optional) | WhatsApp group |

Once you have all required items, write them to `/tmp/aaka-setup.json`:

```json
{
  "telegram_bot_token": "123456:ABC...",
  "telegram_group_id": "-100123456789",
  "telegram_user_id": "123456789",
  "gemini_api_key": "AIza...",
  "whatsapp_phone": "",
  "whatsapp_group_jid": "",
  "vps_host": "203.0.113.10",
  "timezone": "Asia/Kolkata",
  "bot_name": "Aaka",
  "bot_emoji": "🌤️",
  "vault_path": "/Users/yourname/Obsidian/aaka",
  "members": [
    {
      "id": "alice",
      "name": "Alice",
      "telegram_id": "123456789",
      "whatsapp_phone": "+491700000000",
      "role": "admin",
      "namespace": "alice",
      "email": "alice@example.com"
    }
  ],
  "calendars": [
    {"id": "primary", "label": "Family", "emoji": "📅", "private": false}
  ]
}
```

---

## Phase 1 — Mac setup [Claude]

```bash
# Run the non-interactive setup driver
bash admin/setup.sh --config /tmp/aaka-setup.json
```

This step creates the config directory tree, writes `aaka.yaml` and `.env`, installs the Python venv, and generates the SSH executor key.

### Verify

```bash
bash admin/setup.sh --config /tmp/aaka-setup.json --check
# All lines should show [OK] or [SKIP]. No [FAIL].
```

---

## Phase 2 — Google OAuth [Human]

Claude cannot open a browser. You must do this step.

1. `config/credentials.json` is bundled with the repo (community OAuth app — no GCP account needed).
   If you have your own GCP project, place your `credentials.json` at:
   ```
   $AAKA_CONFIG_DIR/tokens/credentials.json
   ```
   Your own credentials take priority over the bundled ones.

2. Run the initial auth flow (opens a browser tab):
   ```bash
   venv/bin/python3 admin/reauth.py
   ```
   Sign in with the Google account that owns your calendar. Accept all requested scopes.
   Google may show "This app is not verified" — click **Advanced → Go to Aaka (unsafe)**. This is normal for open-source desktop apps.

3. Confirm the token was saved:
   ```bash
   ls "$AAKA_CONFIG_DIR/tokens/token*.json"
   # Expected: token.json
   ```

4. Tell Claude: "OAuth done" to continue.

### Verify [Claude]

```bash
venv/bin/python3 -c "
from skills.calendar import gog
cals = gog.list_calendars()
print(f'[OK] Calendar access: {len(cals)} calendar(s) found')
" 2>&1 | head -5
```

---

## Phase 3 — VPS setup [Claude]

SSH to VPS and run deploy. All examples below use the SSH host alias
`aaka-away`, created by `admin/setup.sh` / `admin/deploy.sh`. You can
override the alias name by setting `AAKA_VPS_HOST=<your-alias>` in
`$AAKA_CONFIG_DIR/.env`.

```bash
# From Mac — confirm SSH works first:
ssh aaka-away "echo ok"

# If not yet working, authorize the Mac executor key on the VPS:
cat ~/.ssh/aaka_executor.pub
# Paste the above key into: ssh root@<vps_ip> "cat >> ~/.ssh/authorized_keys"

# Deploy &Away sensor on VPS:
ssh aaka-away "
  mkdir -p /opt/aaka /opt/aaka-config
  cd /opt/aaka
  # Clone repo if not present:
  [ -d .git ] || git clone https://github.com/prasadgupte/aaka-life.git .
  git pull --ff-only
"

# Copy .env to VPS:
scp "$AAKA_CONFIG_DIR/.env" aaka-away:/opt/aaka-config/.env

# Build + launch container:
ssh aaka-away "cd /opt/aaka && docker compose -f docker-compose.prod.yml up -d --build sensor"
```

### Verify [Claude]

```bash
ssh aaka-away "docker inspect -f '{{.State.Status}}' aaka-sensor"
# Expected: running

ssh aaka-away "docker logs aaka-sensor 2>&1 | tail -5"
# Expected: no crash, openclaw gateway start message

curl -sf http://<vps_ip>:18790/health || echo "Health endpoint not yet up"
```

---

## Phase 4 — Telegram bot link [Human]

1. In Telegram, open the group where Aaka should listen.
2. Add the bot (the username you chose in BotFather) to the group as a member.
3. Send `/menu` in the group.
4. Check VPS logs to confirm the message was received:
   ```bash
   ssh aaka-away "docker logs aaka-sensor 2>&1 | grep -i 'router_sensor\|intent\|menu' | tail -10"
   ```
5. Tell Claude: "Telegram linked" to continue.

---

## Phase 5 — WhatsApp QR scan [Human] (optional)

Skip this phase if you don't need WhatsApp.

1. Watch container logs for the QR code:
   ```bash
   ssh aaka-away "docker logs -f aaka-sensor 2>&1 | grep -A 3 -i 'qr\|scan'"
   ```
2. Open WhatsApp on your phone → Linked Devices → Link a Device.
3. Scan the QR code shown in the logs.
4. After pairing, check:
   ```bash
   ssh aaka-away "docker logs aaka-sensor 2>&1 | grep -i 'whatsapp.*ready\|baileys' | tail -5"
   ```
5. Tell Claude: "WhatsApp paired" to continue.

---

## Phase 5b — Signal [Human + Claude] (optional)

Skip if you don't need Signal.

> **Shortcut:** `bash admin/setup_signal.sh` does everything below — it asks
> whether to link to your own account or register a dedicated number, renders
> the QR, handles the captcha, and writes the `.env` keys.

Signal needs **signal-cli** (a Java binary) plus a **dedicated phone number**.
Register a number for aaka rather than linking aaka to your own Signal account:
linking would make aaka *be* your account — it would see all your private
Signal traffic and could never appear as a separate contact in the family chat.

1. Install signal-cli and a JRE — **`bash admin/deploy.sh` does this for you**
   once `signal` is in `ENABLED_CHANNELS` (Homebrew on the Mac, apt plus the
   pinned release tarball on the VPS). Only do it by hand if that fails:
   - Mac: `brew install signal-cli`
   - VPS (Ubuntu): `sudo apt install -y openjdk-21-jre-headless` and unpack a
     signal-cli release into `/opt/signal-cli`
2. Register the dedicated number (once), then verify with the SMS code:
   ```bash
   signal-cli -a +15550000000 register
   signal-cli -a +15550000000 verify 123456
   ```
3. In `$AAKA_CONFIG_DIR/.env`:
   ```
   ENABLED_CHANNELS=telegram,signal
   SIGNAL_ACCOUNT=+15550000000
   ```
4. Install the services where the outbox is flushed — the VPS by default:
   ```bash
   # VPS
   sudo cp deploy/aaka-signal-cli.service deploy/aaka-signal-poller.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now aaka-signal-cli aaka-signal-poller
   # Mac (only with SIGNAL_PLACEMENT=executor)
   bash admin/deploy.sh
   ```
5. Add each family member's Signal number to `aaka.yaml` (`signal: "+E.164"`),
   or onboard them with `/invite <name>`.
6. Verify with `bash admin/diagnose.sh` → the **Signal** block.

Not yet verified against a live Signal account — see `docs/manual.md → Signal`.

---

## Phase 6 — Install Mac daemons [Claude]

```bash
# Install launchd plists (queue worker, DB sync, calendar sync):
bash admin/deploy.sh   # runs Mac branch; idempotent
```

### Verify [Claude]

```bash
for agent in com.aaka.queueworker com.aaka.dbsync com.aaka.calendarsync; do
  launchctl list "$agent" &>/dev/null && echo "[OK] $agent" || echo "[FAIL] $agent not loaded"
done
```

---

## Phase 7 — End-to-end smoke test [Claude]

```bash
bash admin/aaka.sh test
# Expected: all checks pass

bash admin/aaka.sh diagnose
# Expected: environment, config, queue, &Away, &Home all green
```

If anything shows `[FAIL]`:
1. Run `bash admin/aaka.sh diagnose` and read the output carefully.
2. Check VPS logs: `ssh aaka-away "docker logs aaka-sensor 2>&1 | tail -30"`
3. Check Mac queue worker: `tail -20 "$AAKA_CONFIG_DIR/logs/queueworker.log"`

---

## Troubleshooting quick reference

| Symptom | Check | Fix |
|---------|-------|-----|
| Bot doesn't respond in Telegram | `docker logs aaka-sensor` | Check `TELEGRAM_BOT_TOKEN` in `.env` |
| Calendar read fails | `bash admin/aaka.sh cal` | Re-run OAuth (Phase 2) |
| Queue worker not polling | `launchctl list com.aaka.queueworker` | `bash admin/deploy.sh` on Mac |
| VPS container crashes on start | `docker logs aaka-sensor` | Check `.env` has all required vars |
| WhatsApp 440 session conflict | Baileys logs | `bash admin/aaka.sh vps-whatsapp` → option 2 |
| Token expired | `bash admin/aaka.sh auth` | `bash admin/aaka.sh reauth` |

---

## Environment variables

These read from `$AAKA_CONFIG_DIR/.env` (auto-loaded by `admin/lib/detect.sh`):

| Var | Purpose | Default |
|-----|---------|---------|
| `AAKA_VPS_HOST` | SSH host alias for your VPS | `aaka-away` |
| `AAKA_VPS_IP` | Public IP of your VPS — used by deploy + the &home/&away detector | (unset) |
| `AAKA_REPO_URL` | Git URL used by `admin/deploy.sh` when cloning on the VPS | `https://github.com/prasadgupte/aaka-life.git` |
| `ENABLED_CHANNELS` | Comma list of channels to run — `telegram,whatsapp,slack,signal` | `telegram` |
| `SIGNAL_ACCOUNT` | aaka's own Signal number (+E.164) — a dedicated number, not yours | (unset) |
| `SIGNAL_CLI_URL` | signal-cli daemon base URL | `http://127.0.0.1:18794` |
| `SIGNAL_PLACEMENT` | Where signal-cli runs: `sensor` (VPS, default) or `executor` (Mac) | `sensor` |

---

## Re-running after changes

```bash
# Rebuild and redeploy sensor on VPS:
ssh aaka-away "cd /opt/aaka && git pull --ff-only && docker compose -f docker-compose.prod.yml up -d --build sensor"

# Reload Mac daemons after code changes:
bash admin/deploy.sh

# Full health check:
bash admin/aaka.sh diagnose
```
