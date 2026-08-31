# Aaka

**Local-first family calendar assistant on macOS, delivered over WhatsApp and Telegram.**

Aaka helps a small group — your household, your immediate family, or your team —
share a calendar, agree on commitments, and stop losing things in chat. You text
the bot like you'd text a person, it replies with what's on today, schedules
events, tracks tasks, files documents, and reminds people of birthdays.

The goal is "second brain for a few people who actually live together." Not a
SaaS, not a calendar replacement — a thin, opinionated chat surface on top of
Google Calendar and a local SQLite queue.

> ⚠️ **Status:** Personal project, run by one family. Public for transparency
> and so others can self-host or borrow parts. Not packaged for general use yet.
> Expect rough edges.

---

## Architecture: Away / Home

Two processes, one repo:

```
   ┌──────────────────┐         ┌──────────────────────┐
   │  Telegram / WA   │         │   Google Calendar    │
   └────────┬─────────┘         └──────────▲───────────┘
            │                              │
            ▼                              │
   ┌──────────────────┐   queue   ┌────────┴───────────┐
   │  sensor (VPS)    │ ────────► │  executor (Mac)    │
   │  &away           │  SQLite   │  &home             │
   │  - no secrets    │           │  - OAuth tokens    │
   │  - triage intent │           │  - calendar writes │
   └──────────────────┘           └────────────────────┘
```

- **&Away (sensor)** runs natively on your Mac (pure Python — no Docker), or on a
  VPS in Docker when you want always-on. It receives messages, triages
  intent, and writes to a SQLite queue. No Google API calls, no personal
  credentials. Even if the VPS is compromised, your calendar isn't writable
  from it (token scopes are read-only).
- **&Home (executor)** runs natively on your Mac. It polls the queue, performs
  side-effects (calendar writes, file moves), and sends replies. Holds the
  OAuth tokens; never exposed to the internet.
- The split exists so the surface you put on the internet (chat) is separated
  from the surface that holds the secrets (OAuth, password store, Drive).

The queue, config, and tokens live in a directory **outside the repo**
(`$AAKA_CONFIG_DIR`, default `/Users/Shared/aaka-repo-config`). Nothing
sensitive is in git.

---

## What it can do

A compact tour. Full reference in [`docs/manual.md`](docs/manual.md).

| Area | Examples |
|---|---|
| **Calendar** | `/today`, `/week`, `/add physio friday 3pm`, `/block tomorrow 14:00 1h`, `/fix` |
| **Tasks** | `t milk`, `/tasks #school`, `/done 1 3 5`, `/snooze 4 1d`, `/edit 2 due tomorrow` |
| **Lists** | `b groceries milk eggs`, `b groceries ?` (recall what you usually buy) |
| **Notes** | `n ortho appointment with Dr. Sun in May`, `/notes ortho` |
| **Files** | Drop any photo/PDF in chat → auto-route to a per-member vault + compressed |
| **Mail** | `/mail`, `m read work 1`, `/mail fetch` |
| **Birthdays** | `bday`, morning push with one-tap wish links |
| **Engagement** | `/engage` — three-zone ladder, streaks, next-step nudges |
| **Agents** | A pub/sub gateway lets external agents (travel, coach, etc.) send messages and request replies through Aaka's channels |

Single-letter shortcuts: `d` today, `w` week, `t` tasks, `b` lists, `n` notes,
`m` mail, `c` calendar, `f` file-drop, `x` expenses, `p` pdf, `s` status.

Slash commands and natural language both work. Zero-token reads for common
queries (today/week/tasks) — they read pre-rendered Markdown from the executor.

---

## Install

### Quickest path: Claude Code guided setup

The easiest way to get Aaka running is to let Claude Code walk you through it.

```bash
git clone https://github.com/prasadgupte/aaka-life.git ~/aaka-repo
cd ~/aaka-repo
```

Then open Claude Code in this directory and paste:

```
Read CLAUDE_SETUP.md and help me get Aaka running.
```

Claude will run a diagnostic, collect the three things you need (Telegram bot token, Telegram user ID, Gemini API key), walk you through Google OAuth, start the sensor locally, and get your bot responding — no VPS required for a first run.

### Manual short version

```bash
# Everything lives in ~/aaka: code/ (the app) + config/ (your data). No Docker needed.
git clone https://github.com/prasadgupte/aaka-life.git ~/aaka/code
cd ~/aaka/code
python3 -m venv venv && venv/bin/pip install -r requirements.txt -q

# Your data lives separately (updating the code never touches it):
export AAKA_CONFIG_DIR=~/aaka/config
mkdir -p $AAKA_CONFIG_DIR/{config,tokens,data/{queue,calendar},logs}

# Write .env with your tokens (see INSTALL.md), then start — natively, no Docker:
venv/bin/python3 sensor/telegram_multibot.py &   # Telegram sensor (poller)
venv/bin/python3 executor/queue_worker.py &      # processes messages, calendar, etc.
```

> **No Docker required** for a local install — the sensor is pure Python and runs natively.
> Docker is only for the optional always-on VPS.

See [`INSTALL.md`](INSTALL.md) for the full phase-by-phase guide including VPS production setup.

The setup script is non-interactive and idempotent. Re-running with `--check`
is a read-only pre-flight; re-running with `--reset` overwrites existing
config.

---

## Project layout

```
aaka-repo/
├── sensor/               &Away — VPS-side: receive, triage, queue
├── executor/             &Home — Mac-side: poll queue, execute, reply
├── gateway/              channel-agnostic adapter (Telegram + WhatsApp via OpenClaw)
│   └── agent_api.py      FastAPI agent pub/sub gateway (port 18790)
├── skills/               intent handlers (calendar, tasks, drop, notes, mail, ...)
├── aaka_queue/           SQLite queue (butler.db) + schema
├── admin/                setup, deploy, diagnose, test, reauth helpers
├── docs/
│   ├── manual.md         user-facing command reference
│   ├── AGENT-API.md      how external agents integrate
│   └── setup.md          VPS provisioning notes
├── INSTALL.md            step-by-step install for Claude Code
├── cli.md                copy-paste CLI examples per intent
└── CLAUDE.md             working context for Claude Code agents
```

---

## Documentation

| File | What's in it |
|---|---|
| [`INSTALL.md`](INSTALL.md) | Setup driven by Claude Code — credentials checklist, JSON config, phase-by-phase install |
| [`docs/manual.md`](docs/manual.md) | User manual — every intent, every shortcut, examples |
| [`docs/AGENT-API.md`](docs/AGENT-API.md) | How to write an agent that sends messages through Aaka |
| [`cli.md`](cli.md) | CLI dry-run examples for every intent |
| [`CLAUDE.md`](CLAUDE.md) | Project context Claude reads on each session |

---

## Writing your own intent (briefly)

1. Add a row to `skills/registry.yaml` with `intent`, `entrypoint`,
   `runs_on: [sensor|executor]`, and `requires_confirmation`.
2. Implement the handler in `skills/<area>/<name>.py`. Return a dict with at
   least `{"text": "...", "channel_id": ..., "sender": ...}`.
3. Add an intent pattern to `sensor/intents/<domain>.py` so the router matches
   incoming messages.
4. Add a dry-run smoke test to `admin/test.sh`.

A proper Writing Skills guide is in progress.

---

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — source available, personal and
non-commercial use only.

You can read the code, run it for your family, modify it, contribute back,
and use it in research or education. You can't wrap it in a product and sell
it. If you want a commercial license, open an issue.

---

## Contributing

Issues and pull requests are welcome. There is no CLA — by submitting a PR
you agree your contribution is licensed under PolyForm Noncommercial 1.0.0,
same as the rest of the code.

A pre-commit hook lives at `.githooks/pre-commit`. Enable it once with:

```bash
git config core.hooksPath .githooks
```

(`admin/setup.sh` does this automatically.) It blocks commits that introduce
secrets, real personal info, or known leak patterns.
