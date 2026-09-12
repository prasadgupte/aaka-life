# aaka Tools — a plugin layer for scheduled/triggered capabilities

**Status:** spec / proposal. **Goal:** a first-class way to hook capabilities into
aaka — scheduled or on-demand jobs that run sensor- or executor-side, use aaka's
channels + secrets vault, and **always report back (especially errors).** Turns
one-off integrations (homework scraper, wa-backup, website monitors) into a
registered ecosystem with one contract.

## Why

Two failures motivate this: a tool that runs but **goes silent** (family-admin's
homework scraper — no scheduled trigger, no report path), and the temptation to
hand-wire each integration. aaka already has ~80% of the plumbing (agent registry,
`schedule_job`, `send`/`ask`, `scheduled_outbound` that fires with the Mac asleep).
The missing 20% is: **run-dispatch, a reporting/error contract, and placement
routing.** This spec defines those.

## Taxonomy — Skill vs Tool vs Sidecar

Three distinct things (naming them ends a lot of confusion):

| | **Skill** | **Tool** | **Sidecar** |
|---|---|---|---|
| Is | Core assistant capability | Pluggable job/capability | Channel/session adapter |
| Ships | In the repo (`skills/`) | Registered, lives outside core | In the repo (`wa-sidecar/`, `gateway/channels/`) |
| Lifecycle | In-process, always available | Runs on schedule/trigger, then exits | Long-running process owning a live session |
| Extends | What aaka does (built-in) | What aaka does (add-on) | **Where aaka can talk** |
| Example | calendar, tasks, lists | homework check, wa-backup, stock watch | wa-sidecar (WhatsApp), a future signal-sidecar |

**One line:** *Sidecars extend where aaka can talk; Tools extend what aaka can do;
Skills are the built-in Tools.* A Tool *uses* Sidecars (it reports via WhatsApp
through the wa-sidecar); it never *is* one.

## Anatomy of a Tool

A manifest entry (source of truth in `$AAKA_CONFIG_DIR/config/tools.yaml`, managed
via MCP — no hand-editing):

```yaml
homework:
  run: ~/tools/homework/check.py   # script | module | agent invocation
  placement: sensor            # sensor | executor  (see Placement)
  browser: remote              # none | remote | local  (only if it scrapes)
  schedule: "0 7 * * *"        # cron in the family's timezone (aaka.yaml); omit for on-demand only
  command: "/homework"         # optional: a command users can invoke (see Commands)
  report_to: <member-id>       # who hears results — one id or a list [kid, parent]
  report_ok: true              # false → the tool delivers its own output; only errors are reported
  on_error: alert              # alert | digest | silent
  timeout: 120                 # seconds
  secrets: secrets/homework/   # vault path (outside git); passed to the tool
  enabled: true
```

**Run contract** — a tool reads its input (args + `$AAKA_TOOL_SECRETS` dir) and
returns a structured result on stdout (JSON) or exit code:

```json
{ "ok": true, "summary": "2 new: Math (Fri), essay (Mon)",
  "details": "...", "error": null }
```
`error: "auth_required"` is special — see Auth.

## Placement (and the browser escape hatch)

- **sensor** — VPS, always-on, has the send-Gmail. For *lightweight* Python
  (HTTP + login via `requests`/`httpx`, API polling, email digests). Runs
  irrespective of the Mac. Secrets live VPS-side.
- **executor** — Mac. For OAuth tokens, calendar/Drive writes, and *local* browser
  work. Runs when the Mac is on (≈always for a home server).
- **browser: remote** — for always-on *browser* scraping without a heavy VPS, a
  sensor-placed tool drives a **remote Browser Use browser** (`start_remote_daemon`)
  — no local Chromium. This is how "JS-login school portal, checked daily even if
  the Mac is off" works: `placement: sensor, browser: remote`.

Rule of thumb: needs your Google tokens or a fast local browser → executor;
lightweight/always-on → sensor; always-on + needs a real browser → sensor + remote.

## Registration (MCP-first)

Admin-only, conversational — mirrors the member tools already built:
`register_tool(manifest)`, `list_tools()`, `run_tool(name)`,
`enable_tool`/`disable_tool`, `tool_logs(name)`. The manifest persists in
`tools.yaml`; the gateway `agent_registry` issues a key if the tool calls back in.
**Registration is admin-only** — a tool runs code and holds secrets, so it is never
self-installed by an arbitrary caller.

## Commands & namespace (aaka owns routing)

A tool may request a `command`. aaka owns the namespace:
1. **Core intents always win** — a tool command can't shadow `/today`, `t`, etc.
2. **Grant if free** — `/homework` is granted top-level if no core intent/tool claims it.
3. **Collision → prefix** — otherwise it's addressable as `/tool homework …`.
4. `/tools` lists installed tools + their commands + last-run status.

Routing: a matched tool command enqueues a `tool_run` intent `{tool, args, sender}`
→ the placement's runner executes → the result is delivered to `sender`. Because
tools can be slow (scraping), the command **acks immediately** ("⏳ On it — I'll
report back") and the result arrives via the report path. On-demand and scheduled
runs share one dispatch.

## `help` is mandatory

Every tool MUST handle a `help` arg and return its usage + subcommands
(`/tools/<tool> help`). It's how users discover what a tool does without docs, and
it must work without credentials/network. `/tools help` gives the framework
overview. A tool that doesn't self-document is incomplete.

## Executor-placed tools invoked from a sensor-processed command

On a single-machine install the router *is* the executor, so `/tools/<tool>` runs
inline. With a real sensor/executor split, an `executor`-placed tool can't run on
the sensor (no Mac creds/env). So the router **hands off**: it enqueues a
`tool_run` intent and acks *"⏳ running on the home machine — I'll report back"*;
the executor's `_exec_tool_run` runs the tool and writes the result back to the
requester's channel. Placement decides: `sensor` tools run inline on the sensor;
`executor` tools run inline if we *are* the executor, else via the queue. (Scheduled
runs already go to the right side — each side's cron/launchd runs only its own.)

## Who can run tools

Running, listing, and `help` are open to **any recognized family member** — a
parent runs a kid's digest without being the admin. Unrecognized senders are
locked out (`🔒 Only registered members can use tools.`). **Registering,
enabling, and disabling** a tool stays **admin-only** and lives in MCP
(`register_tool`/`set_tool_enabled`) — a tool runs code and holds secrets, so it
is never installed from a chat command.

## Command feedback rule (no group spam, no silent commands)

aaka's reply policy for anything that doesn't match a built-in intent or tool:

- **An explicit `/command` always gets a reply** — even if unknown
  (`❓ I don't know that command. ↪ /menu`). Silence on a command looks broken.
- **Free text in a 1:1 DM** gets a gentle nudge (`Sorry, I didn't understand…`).
- **Free text in a group stays silent** — aaka never spams "didn't understand"
  to ordinary group chatter; it only answers when addressed (a `/command` or a
  matched intent). Silent drops are logged (`silent-drop group free-text`).

So a tool command always produces feedback: the run/hand-off ack, the result, or
the unknown-command hint — never nothing.

## Reporting & error contract (kills the "went silent" problem)

Every run reports — a run that produces nothing still says so:
- **ok** → concise report to `report_to`: *"📚 Homework: 2 new — Math (Fri), essay (Mon)."*
- **nothing new** → *"📚 Homework: nothing new today."* (silence is never the outcome)
- **error** → **loud** alert to the admin per `on_error`: *"⚠️ homework failed:
  login expired. Re-auth: <link>."*

Runs are logged to `$AAKA_CONFIG_DIR/logs/tools/<name>.jsonl` (start, exit, summary,
error) so `/tools` and `tool_logs()` can show history.

Reports reach each recipient on their preferred **enabled** channel (telegram →
whatsapp → signal), so a Signal-only member gets them too. From the executor,
non-Telegram reports are queued to the outbox and delivered by the sensor, where
the Signal/WhatsApp daemons live.

**Missed slots are caught up.** The executor is a laptop that sleeps or is off.
Each scheduler tick also checks whether a tool's most recent slot went by since
its last logged run (within 7 days) and, if so, runs it once, flagged
`catch_up: <slot>` in the log. The latest missed slot only — a machine that was
off for three days does not fire three times. A tool that has never run waits
for its first real slot.

## Auth & the secrets vault

Tool secrets live in the canonical vault `$AAKA_SECRETS_ROOT/<tool>/` —
**totally outside the codebase**, per the standing vault rule (`AAKA_SECRETS_ROOT`
defaults to `~/.aaka/secrets`; set explicitly per install, e.g. on the VPS). The framework resolves the manifest's
`secrets:` name under that root and passes the absolute path as
`$AAKA_TOOL_SECRETS`; the tool never embeds credentials. On `error: auth_required`, the runner `ask()`s the admin to re-auth
with instructions, pauses the tool, and resumes on the next scheduled run.

## Std-built vs pluggable (the boundary)

- **Standard (core skills, in-repo):** calendar, tasks, lists, notes, files,
  budget, mail, pdf, pay, engagement — capabilities *every* aaka wants, tightly
  integrated, always on.
- **Pluggable (Tools):** anything domain/user/family-specific — homework, wa-backup,
  a specific site monitor, a bureaucracy helper. Opt-in, registered, outside core.
- **Graduation:** a broadly-useful tool can be promoted into a core skill later.
- Litmus test: *would every family want it?* → skill. *Specific to yours?* → tool.

## How this organizes `~/tools/`

Two layers, cleanly separated:
- **Capability libraries** (`~/tools/<x>/`) — agent-agnostic utilities
  (md2pdf, pdf-filler, family-pii, google-workspace, wa-backup). Unchanged; shared
  by any agent.
- **aaka Tool registration** — a `tools.yaml` entry that *points at* a capability +
  adds the aaka-specific schedule/placement/report/command. The manifest is the
  index; the code stays in the shared library (or the tool's own dir).

So the shared folder becomes: libraries (reused) + tools (registered). The manifest
tells you, at a glance, what's scheduled, where it runs, and who it reports to.

## Build phases

1. **Dispatch + contract** — `tool_run` intent + the two runners (sensor cron
   `tool_runner`; flesh out executor `_exec_agent_job`); structured-result parsing;
   the report/error path; `logs/tools/<name>.jsonl`.
2. **Manifest + MCP** — `tools.yaml` + register/list/run/enable/logs MCP tools;
   `/tools` command.
3. **Commands & routing** — namespace grant/collision + ack-then-report.
4. **First vertical slice — homework** — exercises every part end-to-end (schedule →
   sensor → remote browser + login → report/error → re-auth). Proves the framework.
5. **Migrate wa-backup** as the second tool (executor, every 6h, alert on session drop).

## Open decisions (for review)

1. **Command syntax** — top-level-if-free-else-`/tool <name>` (recommended) vs always
   prefixed (`/tool <name>`, simpler, no collisions) vs a short prefix (`/x <name>`).
2. **Manifest location** — `$AAKA_CONFIG_DIR/config/tools.yaml` (user-specific,
   recommended) vs in-repo.
3. **Ack UX** — always ack "on it" for on-demand tool commands, or only when a run
   exceeds N seconds.
4. **First slice input** — is the school portal a plain login form (sensor-native
   HTTP) or a JS app (sensor + remote browser)? Decides the first build path.
