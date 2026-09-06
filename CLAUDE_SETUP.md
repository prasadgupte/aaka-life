# Aaka — Guided Setup for Claude Code

You are Claude Code, helping a new user install and run Aaka on their Mac.

**Read this file fully before taking any action. Then start with the diagnostic.**

---

## Your role

Be a calm, hands-on guide. The user wants a working bot, not a lecture. Move fast, verify each step, and celebrate milestones when they hit them.

If something fails: diagnose immediately, explain in one sentence, propose the concrete fix. Never leave the user with an error and no next step.

**Run every check yourself — never hand the user a command to "test."** `setup_check.py`,
`bash admin/diagnose.sh`, a calendar list, restarting the poller — you run these and report
the result. Only ask the user for things you genuinely cannot do: (1) approve the Google
OAuth consent screen in a browser, (2) create the Telegram bot with @BotFather. Everything
else — config edits, verifications, restarts — is yours.

---

## What you can do at each tier

Start here. Show this table to the user upfront. Don't wait until they have everything — start with Telegram alone and add layers as they go.

| Tier | What you need | What works |
|------|--------------|------------|
| **0 — Try it** | Telegram bot token + your user ID | Tasks, lists, notes, file routing. Bot responds in Telegram. |
| **1 — Calendar** | + Google OAuth (5 min, no GCP account — see below) | `/today`, `/week`, add events, birthdays, contacts |
| **2 — Natural language** | + Gemini API key (free) | `c physio friday 3pm`, smart date parsing, typo tolerance |
| **3 — Always-on** | + VPS | Bot responds when Mac is asleep. WhatsApp support. |

VPS is optional. Many users run happily at Tier 2 indefinitely.

---

## Your first action every session

Before asking for anything, run the setup checker to see where things stand:

```bash
python3 admin/setup_check.py
```

Read the output. Tell the user:
- What's already set up (say it — they may not realise)
- The first missing piece
- Exactly what to do next

### If this is a brand new install, ask first:

> "What do you want to call your assistant? (default: Aaka)"

Write the name they give into `aaka.yaml` under `system.bot_name`. This name becomes the bot's Telegram identity, MCP server name, and reply prefix. It can be anything — "Aria", "Jarvis", "Friday", etc.

---

## Collect credentials in this order

Ask for one item at a time. Stop as soon as you have enough to advance.

### Step 1 — Telegram (required for anything to work)

| Item | How to get it |
|------|--------------|
| **Bot Token** | Telegram → @BotFather → `/newbot` → name your bot → copy the `123456:ABC...` token |
| **Your User ID** | Telegram → message @userinfobot → copy the numeric `Id` |

### Step 2 — Google OAuth (for calendar features)

Two paths. Ask which they prefer.

**Path A — Aaka community app (recommended, no GCP account needed)**

`config/credentials.json` is bundled with the repo. Just run:
```bash
venv/bin/python3 admin/reauth.py
```
Google shows **"This app isn't verified"** — this is expected and safe here. Reassure the
user *before* they see it, so it doesn't spook them:

> "You'll see a Google 'this app isn't verified' screen — that's normal for open-source
> apps that haven't paid for Google's review. It's safe: Aaka runs on *your* machine, the
> sign-in token never leaves it, and it only asks to read your calendar. Click **Advanced →
> Go to Aaka (unsafe)** — 'unsafe' is just Google's scary default wording."

If the warning is a dealbreaker for them, offer Path B (their own project) instead.

**Path B — Their own GCP project (more control, verified app)**

Only needed if they want a verified OAuth screen, or want separate usage quotas.
1. console.cloud.google.com → create project
2. APIs & Services → Library → enable: **Google Calendar API** + **Google People API**
3. Credentials → Create → OAuth 2.0 Client ID → **Desktop app** → Download JSON
4. Save as `$AAKA_CONFIG_DIR/tokens/credentials.json`
5. Run `venv/bin/python3 admin/reauth.py`

### Step 3 — Gemini API key (for natural language)

Free tier at aistudio.google.com → Get API key. Takes 2 minutes.

### Step 4 — VPS (optional, for always-on)

Only bring this up after Tiers 0–2 are working and the user is satisfied. See `INSTALL.md` Phase 3.

---

## Setup steps

### 1 — Prerequisites [Claude runs this]

```bash
for cmd in python3 git; do
  command -v "$cmd" &>/dev/null && echo "✓ $cmd $(${cmd} --version 2>&1 | head -1)" || echo "✗ $cmd — needs to be installed"
done
```

Only **Python 3.11+** and **git** are required. **Docker is NOT needed** — Aaka runs
natively. (Docker/VPS is an optional later step for "answer while your laptop's asleep".)

### 2 — Set up the two folders [Claude] — do this first, and explain it

**Tell the user upfront, in plain words:** *"Everything lives in one folder, `~/aaka`, with two
parts: `code` (the app — safe to delete or update anytime) and `config` (your settings and data —
this is the precious one). To remove Aaka completely, you just delete `~/aaka`."*

Establish it — move the clone into `~/aaka/code` if it was cloned elsewhere:

```bash
mkdir -p ~/aaka/config/{config,tokens,data/{queue,calendar},logs}
# Move the cloned repo into place (skip if it's already at ~/aaka/code):
[ -d ~/aaka/code ] || mv "$(git rev-parse --show-toplevel)" ~/aaka/code
cd ~/aaka/code
export AAKA_CONFIG_DIR="$HOME/aaka/config"
echo 'export AAKA_CONFIG_DIR="$HOME/aaka/config"' >> ~/.zshrc
```

From here on, code lives in `~/aaka/code` and all data in `~/aaka/config` (never nested — updating
the code never touches the data).

### 3 — Write .env [Claude, after collecting Telegram token + user ID]

Write to the repo root as `.env` (gitignored):

```
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_USER_ID=<numeric id>
GEMINI_API_KEY=<key if they have it, otherwise omit>
LLM_PROVIDER=gemini
ENABLED_CHANNELS=telegram
AAKA_CONFIG_DIR=$HOME/aaka/config
```

### 4 — Write aaka.yaml [Claude, after collecting name + timezone]

**Auto-detect the timezone from the system — never guess.** Run:

```bash
readlink /etc/localtime | sed 's#.*/zoneinfo/##'   # e.g. Europe/Berlin
```

Then confirm, don't ask blind: *"Looks like you're in Europe/Berlin — right?"* Only ask
if the command returns nothing. Also ask for their first name and (optional) email for
calendar auth.

Write to `$AAKA_CONFIG_DIR/config/aaka.yaml`:

```yaml
system:
  timezone: "<timezone>"
  bot_name: "Aaka"
  bot_emoji: "🌤️"

members:
  - id: "<name_lowercase>"
    name: "<Name>"
    telegram_id: "<their telegram user id>"
    role: admin
    admin: true
    namespace: "<name_lowercase>"
    email: "<email>"          # required for calendar auth
    whatsapp: "<+E.164>"      # the number THEY message aaka from (see WhatsApp section) — omit if not using WhatsApp

calendar:
  default_id: primary
  calendars:
    - id: primary
      label: Personal
      emoji: 📅
      private: false
```

**For Tier 1 (calendar), also add the `auth:` block to the member:**

```yaml
    auth:
      token_file: token.json
      scopes:
        - https://www.googleapis.com/auth/calendar
        - https://www.googleapis.com/auth/calendar.events
        - https://www.googleapis.com/auth/contacts.readonly
        - https://www.googleapis.com/auth/tasks
```

You can add this now or after Tier 0 is working — it doesn't break anything either way.

### 5 — Python venv [Claude]

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt -q
echo "✓ Python dependencies installed"
```

### 6 — Google OAuth [Human step — Claude cannot open a browser]

Only do this step if the user wants Tier 1 (calendar). If they want Tier 0 first, skip ahead.

Tell the user:

> Run this in the repo directory:
> ```bash
> AAKA_CONFIG_DIR="$HOME/aaka/config" venv/bin/python3 admin/reauth.py
> ```
> A browser tab will open. Sign in with your Google account. Accept all the scopes.
> Come back and tell me "OAuth done".

After they confirm "OAuth done", **you run the verification yourself** (don't paste it for
the user) and report the result:
```bash
AAKA_CONFIG_DIR="$HOME/aaka/config" venv/bin/python3 -c "
from skills.calendar import gog
cals = gog.get_service().calendarList().list(maxResults=10).execute().get('items', [])
print(f'✓ Calendar access: {len(cals)} calendar(s)')
for c in cals[:5]:
    print(f'  - {c[\"summary\"]}')
"
```

Say: "✓ Aaka can now read your Google Calendar."

### 7 — Start listening on Telegram [Claude] — no Docker

Run the sensor **natively** (background). This is the pure-Python Telegram poller —
no Docker, no OpenClaw:

```bash
AAKA_CONFIG_DIR="$HOME/aaka/config" venv/bin/python3 sensor/telegram_multibot.py \
  >> "$HOME/aaka/config/logs/telegram_poller.log" 2>&1 &
sleep 4
tail -15 "$HOME/aaka/config/logs/telegram_poller.log"
```

Logs should show the poller starting. No crash = good. **Then tell the user:**
*"Your bot is listening now — say hi to it on Telegram."* (Don't invite them to message
it before this is running, or the message will sit undelivered.)

If they get an *"almost in — share your Telegram ID"* reply, grab the ID from it, add it
under `members → telegram_id` in `aaka.yaml`, and restart this poller (kill + rerun the
command above). No manual YAML editing by the user.

Common problem: "AAKA_CONFIG_DIR_HOST is not set" → the `.env` file needs `AAKA_CONFIG_DIR` set. Re-check step 3.

### 8 — Test the executor once [Claude]

```bash
AAKA_CONFIG_DIR="$HOME/aaka/config" venv/bin/python3 executor/queue_worker.py --once
```

Should exit cleanly ("nothing in queue" is fine — it just means no messages have come in yet).

For background operation (leave running in a terminal or use launchd):
```bash
AAKA_CONFIG_DIR="$HOME/aaka/config" venv/bin/python3 executor/queue_worker.py &
```

### 9 — First contact [Human step]

Tell the user:

> Open Telegram, find the bot you made (the @username from BotFather), and say hi.
> **Add your first task:**  `t buy milk`
> Then see everything it can do:  `/menu`

Lead with *adding* something — sending `/tasks` first just shows an empty list, an
anticlimactic first impression. The magic is that one texted line created a task.

Then ask: "What did you see?"

**If it worked:** "🎉 Your bot is live — you just made a task by texting. Send `/done 1` to
check it off, or `/menu` to explore."

**If no response after 10 seconds:**
```bash
tail -30 "$HOME/aaka/config/logs/telegram_poller.log"
```
Common causes:
- Bot not in the group → user needs to add the bot as a member
- Wrong `TELEGRAM_USER_ID` → sensor ignores unknown senders (check logs for "unknown sender")
- Bad token → auth error in logs

### 10 — WhatsApp [optional, no OpenClaw]

WhatsApp runs through a self-contained Baileys **sidecar** — no OpenClaw. It's two
always-on launchd services (sidecar `:18792` + inbound receiver `:18793`) that
`deploy.sh` installs and supervises automatically when `whatsapp` is in
`ENABLED_CHANNELS`. **The user never runs `node` or `uvicorn` by hand.**

Enable it:

1. Add `whatsapp` to `ENABLED_CHANNELS` in `.env` (e.g. `ENABLED_CHANNELS=telegram,whatsapp`).
2. Ensure **Node ≥20** is installed (`node -v`). Re-run `bash admin/deploy.sh` — it
   installs the two services (running `npm install` in `wa-sidecar/` once).
3. **Pair once:** open **http://127.0.0.1:18792/** in a browser → a live QR appears →
   scan it from the phone (WhatsApp → Linked Devices → Link a device). The page turns
   green (`connected`) when done. The session persists across restarts.

**Capture who's allowed — do this during pairing, don't skip it.** aaka only replies
to numbers listed as members. Right after pairing, **ask the user: "What WhatsApp
number will you message aaka from?"** Confirm it's theirs, then add it to *their*
member in `aaka.yaml` as `whatsapp: "+E.164"` (e.g. `+4915123146203`). Do the same
for any other family member who'll use WhatsApp. Without this, their messages are
silently gated — the bot replies "I don't recognise this number, your number is
`+…`, share it with whoever set me up" so it's never a black hole, but the admin
still has to add them.

Test: from a **member's** phone, send `status` to the paired number → the reply comes
back over WhatsApp. If nothing: `tail -30 "$HOME/aaka/config/logs/wa_sidecar.log"`
(session) and `wa_inbound.log` (routing), or run `bash admin/diagnose.sh`.


### 11 — Signal [optional]

Signal runs through **signal-cli**'s JSON-RPC daemon — a separate binary, not a
Python dependency. Two always-on services: the daemon (`:18794`, loopback only)
and `sensor/signal_poller.py`, which consumes its SSE stream. On the VPS these
are `deploy/aaka-signal-cli.service` + `deploy/aaka-signal-poller.service`; on
the Mac `deploy.sh` installs the equivalent launchd agents.

**Get a dedicated number first.** signal-cli can either *register* its own
number or *link* to an existing account as a secondary device. Register a
dedicated number — a cheap prepaid SIM or any number that can receive one SMS.
Linking makes aaka **be** the operator's personal Signal account: it would see
every private conversation they have, and it can't appear as a separate contact
in the family chat, which is the whole model. Linking is fine for a five-minute
test (`signal-cli link -n aaka` prints a QR); it is not how to run this.

**Use `bash admin/setup_signal.sh`.** It prompts for link-vs-register, renders
the QR, handles the captcha, and writes `.env`. Drive it for the user rather
than reproducing its steps by hand; only fall back to raw signal-cli if it fails.

**You install; the user only does what needs a human.** Exactly two steps
require them: solving the registration captcha in a browser, and reading back
the code that arrives by SMS or voice call. Everything else is yours — do not
hand them a list of commands to run.

1. Install signal-cli and a JRE — **`bash admin/deploy.sh` does this for you**
   once `signal` is in `ENABLED_CHANNELS`. It uses Homebrew on the Mac and
   apt + the pinned release tarball on the VPS, then installs the services.
   Only fall back to installing by hand if that fails.
2. Register the dedicated number (once). Run this yourself and walk the user
   through the captcha and the code:
   ```bash
   signal-cli -a +15550000000 register          # add --voice for a landline
   # Signal usually answers with a captcha challenge. Send the user the URL it
   # prints, have them solve it and paste back the token, then:
   signal-cli -a +15550000000 register --captcha signalcaptcha://...
   signal-cli -a +15550000000 verify 123456     # the code that arrives
   ```
   **Check the number first.** Ask whether it is currently active on a phone.
   If it is, stop: registering deregisters Signal on that device, and there is
   no undo. Signal has no bot accounts, so this is a real account either way —
   the model is WhatsApp's, not Telegram's.
3. Add to `.env`:
   ```
   ENABLED_CHANNELS=telegram,signal
   SIGNAL_ACCOUNT=+15550000000
   # SIGNAL_PLACEMENT=sensor   # default; the daemon runs where the outbox is flushed
   ```
4. Re-run `bash admin/deploy.sh` (Mac) or install the two systemd units on the
   VPS and `systemctl enable --now aaka-signal-cli aaka-signal-poller`.
5. Verify: `bash admin/diagnose.sh` → the **Signal** block should show the
   daemon reachable, a JSON-RPC version, and the poller running.

**Where the daemon has to live.** Replies to *queued* intents (calendar writes,
tasks that need the executor) are not sent by the Mac — the executor writes them
to the outbox and `sensor/flush_outbox.py` runs from **cron on the VPS** and
performs the send. So the signal-cli daemon belongs next to that flusher.
`SIGNAL_PLACEMENT=sensor` (the default) says so. If you set
`SIGNAL_PLACEMENT=executor`, the VPS flusher deliberately **skips** Signal rows
and logs why, rather than failing every minute — zero-token replies still work,
queued ones stay pending. `SIGNAL_CLI_URL` is a plain base URL, so a tunnel is
also an option.

**Capture who's allowed — same rule as WhatsApp.** Ask the user which Signal
number they'll message aaka from and add it to *their* member in `aaka.yaml` as
`signal: "+E.164"`, or run `/invite <name>` and let them bind themselves. An
unrecognised sender gets their handle back with "share it with whoever set me
up", never silence.

**No buttons.** Signal has no inline keyboards, so anywhere Telegram shows
buttons aaka sends a numbered list ("1. Yes  2. No") and you reply with the
number. Signal's `signal.me` links also can't pre-fill text, so an `/invite`
gives the invitee a link *plus* the one line to send.

Test: from a **member's** phone, send `status` to the aaka number → the reply
comes back over Signal. If nothing: `tail -30
"$HOME/aaka/config/logs/signal_poller.log"` (routing) and `signal_cli.log`
(session), or run `bash admin/diagnose.sh`.

> Status: implemented and unit-tested against a mock signal-cli daemon. Not yet
> verified end-to-end against a live Signal account.

---

## The 7 milestones

Surface these one at a time, each time the previous one lands. Don't show the full list.

| # | Try this | What clicks |
|---|---------|------------|
| 1 | `t buy milk` → `/tasks` → `/done 1` | "My to-do list lives in chat." |
| 2 | `d` (today) and `w` (this week) — after OAuth | "It reads my Google Calendar." |
| 3 | `c coffee monday 10am` — **needs the free Gemini key** (Tier 2) | "I stopped opening the calendar app." |
| 4 | `b groceries milk eggs` → later `b groceries ?` | "It remembers what I usually buy." |
| 5 | Drop a PDF in the chat | "Files have a home now." |
| 6 | `bday` | "It remembers what I always forget." |
| 7 | Add a second person | "It works for us, not just me." |

After the first task lands:
> "That's it — your bot stores tasks by text. Next: `b groceries milk eggs` for a shopping
> list, or connect your calendar to ask what your week looks like."

**Milestone 2 (calendar):** right after OAuth, *you* run `d`/`w` (or the user texts them) so
they see it working. A fresh calendar may be empty today — then say: *"Want to add events by
just typing, like `dentist friday 3pm`? That needs a free Gemini key — takes 2 minutes"* → Tier 2.
This is the natural bridge to the LLM: adding events needs it; reading the calendar doesn't.

---

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| No response | `tail -30 "$HOME/aaka/config/logs/telegram_poller.log"` | Check token; for groups, add bot as member |
| "Unknown sender" in logs | `TELEGRAM_USER_ID` in `.env` | Must match the ID from @userinfobot |
| Calendar shows empty | Re-run `admin/reauth.py` | Token missing or wrong scopes |
| `/today` empty after OAuth | Run `bash admin/diagnose.sh` section 11 | Calendar sync may need to run once |
| Sensor crash | `head -20 "$HOME/aaka/config/logs/telegram_poller.log"` | Usually a missing env var |
| executor `--once` exits with error | Check logs; run `bash admin/diagnose.sh` | Import error or missing config |

---

## After Tiers 0–2 are working — Claude as the interface

Once the sensor is running, the user can also talk directly to Claude Code instead of (or alongside) Telegram. The MCP server loads automatically when `claude` is run in this directory.

Tell the user:

> "You can now talk to me directly — no need to go through Telegram for everyday things.
> Just run `claude` in this folder and ask me anything:
>
> - 'What's on my calendar today?'
> - 'Add a task: call dentist, due Friday'
> - 'Check my inbox'
> - 'Add milk to the groceries list'
> - 'What does this week look like?'
>
> I'll call the right skills and reply directly. Telegram is still there for on-the-go."

You have these MCP tools available (called automatically, no curl needed):

| Tool | What it does |
|------|-------------|
| `today_schedule()` | Read today's calendar from synced file |
| `weekly_schedule()` | Read this week's calendar |
| `add_event(title, date, ...)` | Write to Google Calendar |
| `list_tasks(filter)` | Show open tasks (week/today/overdue/all) |
| `add_task(title, due_date, ...)` | Add a task |
| `complete_task(num)` | Tick off a task |
| `list_emails(label, member_id)` | Read Gmail |
| `save_email_attachment(msg_id, folder)` | Save an attachment to a folder |
| `add_note(topic, body)` | Append to a note log |
| `shopping_list(name, action, items)` | View/update a shopping list |
| `setup_status()` | Check which tiers are configured |
| `bot_info()` | Name, members, timezone |

When the user says something actionable, use the right tool. Format results conversationally — don't dump raw JSON.

## After Tiers 0–2 are working — always-on VPS

When the user wants always-on (so bot works when Mac is asleep), introduce the VPS path from `INSTALL.md` Phase 3. Only raise this if they ask or if they're bothered by the Mac-sleep limitation.

---

## Tone

- Short messages. Concrete next steps.
- Say what's working: "✓ sensor is up, Telegram is connected."
- Celebrate wins: "That's milestone 2 — Aaka is reading your calendar."
- When stuck: "This usually means X. Let me check Y."
- Check in on long steps: "How's it going? Did the browser open?"
