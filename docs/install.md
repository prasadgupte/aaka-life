# Install Aaka via Claude Code

This file is the bootstrap guide for Claude Code. When a user shares this URL,
Claude reads these instructions and drives the full install.

**Short URL:** `aaka.life/install` → this file

---

## Instructions for Claude

You are helping a user install Aaka — a personal calendar and task assistant
delivered over Telegram (and optionally WhatsApp). Read these instructions
fully, then start the install.

### Step 1 — Clone the repo

```bash
git clone https://github.com/prasadgupte/aaka-life.git ~/aaka
cd ~/aaka
```

### Step 2 — Open Claude Code in the repo

```bash
claude
```

Claude Code will automatically read `CLAUDE_SETUP.md` and guide the user
through the rest of the setup interactively.

---

## What the user gets

| Tier | What's needed | What works |
|------|--------------|------------|
| **0** | Telegram bot token + user ID | Tasks, lists, notes. Bot responds in Telegram. |
| **1** | + Google OAuth (bundled, no GCP account) | `/today`, `/week`, add events, contacts |
| **2** | + Gemini API key (free) | Natural language: "add physio Friday 3pm" |
| **3** | + VPS | Always-on, bot responds when Mac is asleep. WhatsApp. |

VPS is optional. Most users run at Tier 2.

---

## Exposing a page safely (optional)

By default aaka exposes **nothing** to the internet — the sensor is poll-only (no
inbound port) and web pages (e.g. the taskboard) bind `127.0.0.1`. You never have
to secure a VPS you didn't open up.

If you *do* want to reach a page from outside, aaka's portable guard (`webauth.py`)
protects it **without depending on any proxy**:

- A page server **refuses to bind to a public interface** unless a secret is set —
  so you can't accidentally expose it unauthenticated (a proxy in front is not
  enough; a direct hit to the port bypasses it).
- To allow it: `python3 admin/page_auth.py init` (writes a 600 secret), then start
  the page with `--host 0.0.0.0`. Sign in by opening it once with `?k=<secret>`.
- Already behind your own authenticating proxy? Leave the secret unset and bind
  `127.0.0.1` — the guard stays dormant, no double auth.

---

## Requirements

- macOS (Apple Silicon or Intel) — or Linux
- Python 3.11+ and git
- Claude Code CLI: `npm install -g @anthropic-ai/claude-code`
- A Telegram account
- *(Docker is **not** required — only for the optional always-on VPS)*

Time to Tier 0: ~15 minutes.

---

## After setup — Claude as your assistant

Once installed, run `claude` inside the `~/aaka` directory.
Claude loads the MCP server automatically and you can ask it anything:

> "What's on my calendar today?"
> "Add a task: call dentist, due Friday"
> "Check my email"
> "Add olive oil to the groceries list"
> "What's this week look like?"

No Telegram required for this mode. Claude is the interface.
