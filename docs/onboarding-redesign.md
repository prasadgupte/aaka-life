# Onboarding / Fresh-Install Redesign — Feedback Consolidation

**Living doc.** Consolidates fresh-install UX feedback (PG testing on a work computer)
to feed a full build. **Not implemented yet** — this is the capture + analysis layer.

---

## Session 2026-08-31 — first fresh-install run (work computer, from `main`)

### What the setup showed the user (verbatim)
> **What's already working:**
> - Docker is running
> - Bundled Google OAuth credentials are present
>
> **What's needed to get started (Tier 0):**
> The bot needs a few credentials from you. I'll collect them one at a time.

### Feedback (PG)
1. **Jargon** — "Docker" and "Bundled Google OAuth credentials" mean nothing to most users; opaque and slightly scary.
2. **Docker probably isn't needed** — for a local install, why is Docker even mentioned as a requirement?
3. **"OAuth credentials" is scary** — we really just mean *an app registration to authenticate against* (to read your calendar). Say it in plain words.
4. **"Tier" is too technical** — internal framing, not user-facing.
5. **Wanted experience** — an MCP-style, feature-first status that shows:
   - what's **enabled / working now** (e.g. tasks),
   - what it **can already do**,
   - what's **next** (e.g. "let's securely connect your calendar"),
   - or **asks which features** they'd like to turn on (e.g. "talk to it on Telegram").

### Analysis (Claude)
- **Docker is NOT needed for a local install.** `setup_check` lists `docker`+`sensor` under Tier 0 only
  because the sensor (Telegram poller/router) historically ran in a container even locally. The
  OpenClaw removal made the sensor **pure Python**, so it can now run natively
  (`python3 sensor/telegram_multibot.py`). → Redesign should default to a **Docker-free local path**;
  Docker/VPS becomes an *optional* "always-on" upgrade.
- **Language reframes** (from → to):
  - "Docker is running" → drop from the user view (or fold into an optional "always-on" feature).
  - "Bundled Google OAuth credentials are present" → *"Calendar is ready to connect — the app registration is
    already included; you just approve it once."*
  - "OAuth credentials" → *"connect your Google Calendar — you approve once, and nothing leaves your machine."*
  - "Tier 0/1/2/3" → **feature milestones**, not tiers.
- **Progressive, feature-first onboarding** (the MCP-style ask): reframe from "requirements per tier" to
  *"what would you like {name} to do?"* backed by a **live capability board**:
  - ✅ **Already works:** tasks · lists · notes
  - 🔌 **Turn on when you're ready:** "Chat on Telegram" · "See your calendar" · "Understand plain language" · "Answer while your laptop's asleep"
  - Each shown as a **benefit** + one plain-language line of what it needs.

### Build implications (to plan)
- Replace the **tier table** with a **capability board** (enabled / available / benefit-framed).
- **Docker-free local default** — now unblocked by the OpenClaw-free Python sensor.
- Rewrite `CLAUDE_SETUP.md` + `setup_check.py` language (jargon → plain; features → not tiers).
  (Folds into OpenClaw-removal **Phase 5** doc pass.)
- Consider surfacing the capability board via an **MCP tool** (`setup_status()` already exists in
  `mcp/server.py`; `admin/setup_check.py --json` is the data source to reshape).
- Drop the stray `openclaw-data` dir from the Tier-0 `next_fix` (vestigial).

---

## [append future feedback below this line]
