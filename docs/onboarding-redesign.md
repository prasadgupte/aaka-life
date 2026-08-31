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

## Session 2026-08-31 (cont.) — timezone guess + self-authorization

### Feedback (PG)
1. Setup said *"using Asia/Kolkata as the timezone"* — **how did it guess that?** Should read the actual
   system timezone via a local command, not guess.
2. Messaged the fresh bot: **delivery delay** (message sat undelivered, then delivered, **no read mark**),
   then got the unknown-sender reply: *"Your Telegram user ID is: 123456789. Add it to your aaka.yaml under
   members → telegram_id, then restart the sensor."*

### Analysis (Claude)
- **Timezone — detect, don't guess.** The right value comes from the OS in one line:
  `readlink /etc/localtime | sed 's#.*/zoneinfo/##'` → e.g. `Europe/Berlin` (confirmed on this machine).
  Python fallback: `os.readlink('/etc/localtime').split('zoneinfo/')[-1]`, else `time.tzname`. Setup should
  auto-detect and **confirm** ("Looks like you're in Europe/Berlin — right?"), never hardcode Asia/Kolkata.
- **Self-authorization is far too technical.** *"Edit aaka.yaml → members → telegram_id, then restart the
  sensor"* asks a non-technical user to hand-edit YAML and restart a service. The onboarding (Claude)
  already knows who's installing, and the unknown-sender reply **even prints the user's ID** — it should
  **capture that ID and authorize the installer automatically**, then **hot-reload** (no manual restart).
  Ideal flow: *"Message your bot now and I'll grab your ID and add you."* → round-trip works, done.
- **Delivery / read marks.** Telegram **bots don't show read receipts** (the blue double-check), so "no read
  mark" is normal-but-confusing. The initial delay suggests the **poller wasn't running yet** when they first
  messaged (Telegram held the backlog until polling started). Onboarding should **confirm the poller is live
  before** inviting the user to message it ("your bot is listening now — say hi").

### Build implications (to plan)
- **Auto-detect timezone** via local command; present as a confirm, not a guess.
- **Auto-capture + authorize** the installer's Telegram ID; **hot-reload** config — kill "restart the sensor".
- Make **poller-start explicit** so there's no dead-air before the first reply.
- The "Chat on Telegram" capability (from the board) must end in a **working round-trip**, never a YAML edit.

---

## Session 2026-08-31 (cont.) — fixes applied + MCP gap

**Q: do we control the jargon?** Yes — the install words come entirely from *our* files
(`CLAUDE_SETUP.md`, `admin/setup_check.py`, and hard-coded source strings like the
unknown-sender reply in `sensor/router_sensor.py`). Claude follows the script we write.

**Fixes shipped this session** (branch `feature/openclaw-out`):
- `setup_check.py`: **Tier 0 is Docker-free** — dropped `docker`/`sensor`(container),
  added `check_sensor_native` (runs `sensor/telegram_multibot.py` natively). Labels
  reframed off "Tier N" → "Chat on Telegram", "See your calendar", etc.
- `CLAUDE_SETUP.md`: prereqs drop Docker (Python+git only); **timezone auto-detect**
  (`readlink /etc/localtime`, confirm not guess); sensor start is **native, no Docker**;
  poller-start made explicit ("say hi *now*"); self-auth reframed (grab ID, no user YAML edit).
- `router_sensor.py`: unknown-sender reply softened — *"you're almost in — share your
  Telegram ID"*, no "edit aaka.yaml / restart the sensor".

**Still open (bigger, for the full build):**
- **Capability-board onboarding** (feature-first, MCP-style) — the setup_check reframe is a
  down-payment; the full "what would you like to turn on?" board is still to design.
- **Self-authorization without any restart** — currently still needs the native poller to
  reload config (kill+rerun). Add config hot-reload so adding a member is instant.
- **MCP server is absent from aaka.life** — the "run it via Claude" half of the Claude-native
  story (`mcp/server.py`: *"Claude, what's on my calendar this week?"* → aaka's Python).
  Should become a site section under the Claude-native pillar. *(New site TODO.)*

## Session 2026-08-31 (cont.) — install layout decided

**Decision (PG):** one **visible root** with sibling folders, and Step 1 explains it upfront:
```
~/aaka/
  ├── code/     ← the git clone (disposable — delete/update anytime)
  └── config/   ← aaka.yaml, tokens, data (precious)
```
- Code/config stay **separate** (siblings, never nested) — updating code never touches data.
- One folder the user owns: backup = copy `~/aaka/config`; **uninstall = `rm -rf ~/aaka`** (fixes the
  earlier "delete = two paths, one hidden" confusion).
- Repo folder is named **`code`** (not `repo`).

**Shipped:** `CLAUDE_SETUP.md` Step 2 establishes `~/aaka/{code,config}` and explains it in plain words;
all `~/.aaka` → `~/aaka/config`; `.env` template dropped `GATEWAY_BACKEND=openclaw`, added
`LLM_PROVIDER=gemini` + `ENABLED_CHANNELS=telegram`. `setup_check.py` default is now `~/aaka/config`.

## [append future feedback below this line]

## Session 2026-08-31 (cont.) — real bugs from PG's native install

- **Auth wall was a field-name bug**, not int/string or restart: setup writes the member
  field `telegram_id`, but `aaka_config.member_by_sender` matched on `telegram`. So the ID
  never matched → "you're almost in". Fix: match on `telegram_id` OR `telegram` (str-coerced,
  so unquoted YAML ints also match).
- **Config path is confusing**: `AAKA_CONFIG_DIR=~/aaka/config` → aaka.yaml at
  `~/aaka/config/config/aaka.yaml` (double `config`). Didn't cause this failure but is a smell;
  make `_load` tolerate `$AAKA_CONFIG_DIR/aaka.yaml` too, or rethink the layout.
- **Claude should run verifications, not the user.** It handed PG a calendar-test command to
  paste. CLAUDE_SETUP now says: run every check yourself (setup_check / diagnose.sh / calendar
  list / restart); only ask the user for browser OAuth consent + creating the Telegram bot.
