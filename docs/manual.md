# Aaka User Manual

Use cases and how to interact with the assistant via Telegram, WhatsApp or Signal.
Every command below works the same on every channel — see **Channels** at the
bottom for the handful of places a channel's own limits show through.

---

## Reading the calendar

```
/today          — What's on today?
/week           — What's on this week?
today           — (natural language also works)
what's on tonight?
```

Response is instant (zero LLM tokens) — reads a local Markdown file.

---

## Day schedule

```
/day Mon                — detailed schedule for next Monday
/day tomorrow           — tomorrow's schedule
/day 2026-05-05         — schedule for a specific date
```

Shows a detailed day view with time slots and events.

---

## Adding events

```
/add physio friday 3pm
/add dentist appointment tuesday 10am with Mama
/add team lunch next thursday 12:30-14:00
```

The assistant extracts event details and sends a preview:
```
📅 Physio
   Friday 2026-03-20  15:00–16:00

Confirm? Reply: yes / cancel
```

Reply `yes` or 👍 to save, `cancel` or 👎 to discard.

---

## Blocking time

```
/block focus tomorrow 9-12      — block focus time
/block study friday 14-17       — block study time
```

Creates a calendar block. Confirm to save.

---

## Fixing calendar issues

```
c fix                   — show all issues (carrier, overlap, ghosted)
w #fix                  — weekly view with inline fix annotations
c fix 3                 — detail card for issue #3
c fix 3 Alex          — assign Alex as carrier for issue #3
c fix 3 #work           — add to work calendar (private visibility)
c fix 3 #work Alex    — assign carrier + add to work calendar
```

Issues are numbered. Types: ❓ missing carrier · 🧱 overlap · 👻 not accepted.

---

## Adding tasks

```
/addtask buy groceries by friday
/task call dentist tomorrow
remind me to send invoice by end of week
```

Tasks are stored locally on the sensor — no Google Tasks dependency.

---

## Task management

```
/tasks                  — list open tasks
/tasks #errands         — filter by tag
/tasks @alice           — filter by owner
/tasks overdue          — only overdue tasks
/tasks today            — due today
/tasks week             — due this week
/tasks nodate           — tasks without a due date (inbox)

/done 1                 — mark task 1 done
/done 1 3 5             — bulk complete
/done today             — show completed today
/done week              — show completed this week

/edit 2 friday          — change due date
/edit 2 @bob            — reassign owner
/edit 2 !high           — set priority (high/medium/normal)
/edit 2 new title       — change title

/del 3                  — permanently delete task

/snooze 1 3d            — snooze task 1 for 3 days
/snooze 1 tomorrow      — snooze until tomorrow
/snooze 1 1w            — snooze for 1 week
/snooze all 3d          — snooze all visible tasks
/snooze overdue 1d      — snooze only overdue tasks
```

Recurring tasks auto-recreate with the next due date when completed.

Tasks without a due date are surfaced in the morning push after 7 days as a nudge to triage.

---

## Birthdays

```
/bday                   — upcoming birthdays (next 7 days)
bday                    — same
```

---

## System status

```
/status         — health check + last sync time
status          — (natural language)
```

## Introspection — `/mcp` (admin only)

Read-only views of what aaka knows, from chat. These are the **same views the
MCP read tools expose** to Claude Code — chat isn't an MCP client, so `/mcp`
doesn't proxy the protocol; both front-ends call the same underlying functions,
so the data never drifts.

```
/mcp                        — list the introspection views
/mcp members                — family roster · role · admin · source (yaml|invite)
                              · handles, with ✅ = recognized (reachable on some channel)
/mcp members invite <name>  — mint a one-time code + wa.me link to onboard someone
/mcp members add <name>     — create a member (no code yet)
/mcp tools                  — registered tools · enabled · schedule (same as /tools)
/mcp bot                    — assistant name · timezone · member count
/mcp setup                  — setup status across tiers
```

**Delightful first contact:** when an invited person sends their code, aaka binds
their handle and replies greeting them **by name + a gist of today** from the family
calendar (weather, today's events, birthdays) — so the very first message already
feels personal and useful. `invite` here is the same flow as the top-level `/invite`
command; use whichever you like.

`who` is an alias for `/mcp members`. Registering/enabling/disabling tools or
members stays out of this read surface — that's admin-only via MCP (`register_tool`,
`add_member`, …) or the dedicated commands. (0 tokens)

## Error digest (admin only)

Persistent store of sensor errors — survives across digest runs so nothing is lost before you act on it.

```
/errors              — show unacknowledged error counts by category
/errors flush        — mark all current errors as acknowledged (resets the digest)
/errors scan         — scan logs now and store new events immediately
```

**Daily digest** (07:30 UTC, auto-sent) reports only unacknowledged errors. Use `/errors flush` after reviewing to get a clean next digest. Week-over-week trending is tracked in `error_events` SQLite table.

**Breaking errors vs delivery warnings.** The digest separates *breaking* errors
(router dispatch failures, poller crashes — counted in the **Total**) from
*delivery warnings* (`tg_delivery`/`tg_other`: Telegram 429 rate-limits + transient
getUpdates network blips). Warnings are shown muted and **not** counted, and a
digest containing *only* warnings is suppressed — so you're pinged for things that
actually break, not for delivery noise. `/errors` always shows the full list.

---

## Engagement report

Shows your current engagement level, which features you've tried, and what to try next.

```
/engage
engage
```

**Output includes:**
- Current level (0–5) with label, e.g. *Level 3: Calendar owner*
- Total commands sent
- Features used with frequency (e.g. "Today's schedule — 9×")
- Features not yet tried
- Specific next step to reach the next level

**Levels:**

| Level | Label | Unlock condition |
|-------|-------|-----------------|
| 0 | Newcomer | Just started |
| 1 | Morning habit | Checked today/week schedule 3+ times |
| 2 | List keeper | Used shopping lists |
| 3 | Calendar owner | Added or fixed an event |
| 4 | Organised | Used tasks or planning |
| 5 | Power user | Used notes or file drops |

Level is computed automatically from your usage history — no manual tracking needed.

---

## Single-letter shortcuts

No need to type `/` — just use the letter:

| Letter | Expands to | Example |
|--------|-----------|---------|
| `d` | `/today` | `d` |
| `w` | `/week` | `w` |
| `c` | `/cal` | `c dentist friday 3pm` |
| `n` | `/note` (write) or `/notes` (read) | `n fin 2025 claim boston taxi` · `n fin` |
| `f` | `/drop` (file) | `f flo172` (with photo attached) |
| `b` | `/buy` | `b rossmann toilet paper 2x` |
| `x` | `/expense` | `x 45 groceries lidl` |
| `t` | `/tasks` (list) or `/addtask` (create) | `t` · `t call dentist by friday` |
| `s` | `/status` | `s` |
| `q` | queue status | `q` |

---

## Reminders (recurring, contextual)

Standing prompts that appear in that day's schedule and morning brief — "sport
bag on days with sport". They are **not tasks**: nothing to tick, nothing goes
overdue. Use tasks for anything you want tracked to completion; use a reminder
for something only useful on the morning it applies.

Set them up by asking Claude (MCP), not by editing YAML:

> "remind me about Ari's sport bag on days with sport"
> "Rumi has sports on Mondays — remind about regular shoes, not on holidays"

Two conditions, combinable:

| Condition | Meaning |
|---|---|
| `weekday` | `mon`, or `mon,wed` |
| `event_matches` | Regex over today's events, e.g. `sport\|PE` |
| `unless_matches` | Suppresses it, e.g. `holiday\|no school` |

**Prefer `event_matches` when the reminder depends on an activity.** It follows
the calendar, so it keeps working when sport moves to another day. A weekday
rule silently goes wrong when the timetable changes, and a reminder that fires
on the wrong day costs you trust in all of them.

`preview_reminders` shows what would fire on a given date against the real
calendar — worth running once after adding a rule. Matching is deterministic and
costs **zero tokens**: the LLM helps you write a rule, and is never involved in
evaluating one.

Stored in `config/reminders.yaml`, managed like `tools.yaml`.

## Notes & file drops

```
n fin                            — read recent entries in fin log
n fin 20                         — read last 20 entries
n fin 2025 claim boston taxi     — append to fin.md log
n todo call dentist              — append to todo.md log
/notes                           — list all note topics
/tags                            — list all tags (people, properties, docs…)
f flo172 invoice                 — file an attached photo/PDF
f help                           — drop help with tag ideas
```

`n <topic>` with no body = read. `n <topic> <text>` = write. No ambiguity.
Notes append to a per-topic log file (`vault/Notes/<you>/fin.md`). No file explosion.

**Naming:** Files are renamed to `YYMMDD_tags_original.ext` (e.g. `260504_tax_receipt.pdf`) with a companion `.meta.md` for full metadata.

**Note + file combo:** Send `n tax 2025 receipt` with a photo/PDF attached — writes the note AND files the attachment. The note entry includes an inline file reference: `📎 260504_tax_receipt.pdf`.

**Auto-routing:** When tags match entities + actions in `references.yaml`, files go directly to the right vault folder:
- `f flo172 expense` → `_shared/properties/flo172/expenses/`
- `f flo172 tax` → `_shared/properties/flo172/tax/`
- `f flo172 invoice` → `_shared/properties/flo172/invoices/`
- Unrecognized tags → `00-Inbox/<you>/` (with a warning)

**Wrong folder?** Use `/undo #hash` to move the file back to Inbox, then re-drop with correct tags.

**Smart drop prompt:** Send a photo/PDF without `/drop` and the bot asks what to do:
- Tap a suggested tag button (from your note topics) to file it instantly
- Type tags manually (e.g., `tax invoice`) to file with those tags
- Tap *Skip* or type `skip` to discard

---

## Shopping / lists

```
b rossmann toilet paper 2x      — add item to rossmann list
b rossmann                      — show the list (numbered for check-off)
b rossmann done toilet          — check off matching item
b rossmann #clear               — archive done items (preserves history)
b                               — show all lists
```

### Ideas (recall what we usually buy here)

```
b rewe ?                        — show numbered "ideas" pool:
                                  items previously bought here (✓ -7d)
                                  + seed entries from _seed/ideas.md (seed)
b rewe add 1 3 5                — add ideas #1, #3, #5 to the active list
                                  (or just reply  1 3 5  after the ? view)
```

Ideas are auto-built from:
1. Every `- [x]` you've ever checked off on this list (active + `## archive` sections), deduped, most recent first.
2. A user-maintained seed file at `$AAKA_CONFIG_DIR/data/lists/_seed/ideas.md` — sections like `** Food`, `** Drugs`, `** Fresh` map to markets via `SEED_CATEGORY_TO_MARKETS` in `skills/lists/list_manager.py`. WhatsApp paste format (timestamps, bullets, sub-headings) is normalized at read time, so you can paste raw chat exports.

Items currently open on the active list are excluded from ideas to avoid duplicate adds. `#clear` archives done items rather than deleting them so the ideas pool keeps learning.

Lists are stored on Away for instant response — no 60s Home wait.

---

## Budget / expenses

```
x 45 groceries lidl             — log EUR 45, category=groceries, merchant=lidl
x 120 utilities                  — log EUR 120, no merchant
x                                — current month summary (category totals + bar chart)
x april                          — April summary
x 2026                           — yearly summary (month-by-month)
x undo                           — remove last entry
x fix 3 55                       — change entry #3 amount to 55
```

Stored per-month in `data/budget/YYYY-MM.md`. Zero-token — no LLM needed.
Synced to vault via file_sync.

---

## Undo / cancel queued actions

Cancel a pending or confirmed action, or undo an already-executed calendar event:

```
undo #9a9a8ce0          ← cancel a pending/confirmed queue item
undo #329f3b7b          ← if already written to calendar, deletes the event(s) too
```

- Pending/confirmed items → status set to `cancelled`
- Done calendar events → events deleted from Google Calendar, status set to `cancelled`
- The `#hash` is shown in every confirmation and reply message

---

## Gmail label skills

Gmail emails matching configured labels are automatically ingested, extracted via LLM, and
routed to the appropriate output — no user command needed.

Configure in `$AAKA_CONFIG_DIR/config/aaka.yaml`:

```yaml
gmail_labels:
  - label: INVOICES          # Gmail label name (exact, case-sensitive)
    prompt: "Extract vendor, amount, and due date from this invoice email. Be brief."
    output: note             # telegram | note | task
    topic: fin               # required for output=note — appended to fin.md
  - label: SCHOOL
    prompt: "Summarise this school notice in 2 sentences."
    output: telegram         # posts to group chat
  - label: URGENT
    prompt: "What action is required, and by when?"
    output: task             # creates a local task
```

- **output=telegram**: posts `📧 LABEL — <extracted text>` to the group chat
- **output=note**: appends LLM result to `n <topic>` log (data/notes/<topic>.md)
- **output=task**: adds a task to the local task store
- Polls every 10 minutes; first run bootstraps (marks all existing as seen, no flood)
- Body extraction: prefers `text/plain`, strips HTML if needed, caps at 3000 chars
- **Token**: requires `gmail.readonly` scope in VPS `token_aakash.json`

---

## Menu

```
/menu           — list all available commands
```

---

## Design notes

- Zero-token intents (`t`, `w`, `s`, `j`, `/menu`, `b`, `n <topic>`, `/tasks`, `/done`, `/snooze`, `/day`, `/bday`) respond instantly
- LLM extraction intents (`c`, `j <text>`, `/addtask`, `/block`) use ~200–400 tokens for parsing only
- All data stays local — no personal data leaves your infrastructure
- Lists sync to vault in background via debounced `file_sync` queue items

---

## PDF Tool

Manipulate PDF files directly from Telegram. Attach a PDF (or images for merge), send a command, and receive the result file back.

**Commands:**

| Command | What it does |
|---------|-------------|
| `p compress` | Reduce file size (stream compression + image recompression) |
| `p extract` | Extract per-page text → receive a .txt file |
| `p split 1,5,9` | Split at pages 1, 5, 9 → 3 PDFs (4 pages, 4 pages, 2 pages) |
| `p split 3-5` | Extract pages 3–5 only |
| `p split 2s` | Every 2 pages → a separate PDF |
| `p merge` | Merge all staged PDFs/images into one (send files first, then this) |
| `p delete 3-5` | Remove pages 3–5 |
| `p delete 1,3,5` | Remove specific pages |
| `p delete 2s` | Remove every 2nd page |
| `p delete blank-pages` | Auto-detect & remove blank pages (typical for duplex scans); returns a `*_blanks.pdf` to verify nothing real was discarded |
| `p delete blank-pages dont-return` | Same, but skip the verification `*_blanks.pdf` |
| `p ocr` | OCR a scanned PDF via Tesseract → receive a `.txt` file |
| `p compress ocr` | Append `ocr` to any command to OCR its output last (chains after compress, split, delete, etc.) |
| `p help` | Show full command reference |

**Page spec syntax:**
- `1,5,9` — split-points (for split) or specific pages to remove (for delete), 1-indexed
- `3-5` — range inclusive
- `2s` — every N pages stride

**Merge workflow:**
1. Send each PDF/image file individually (they are staged automatically)
2. Send `p merge` (no attachment) to combine them in order
3. Files expire from the merge stage after 10 minutes

**Shortcut:** `p` expands to `/pdf` (e.g. `p compress`, `p split 2s`)

**Agent SDK:**
```python
from gateway.agent_client import AakaClient
client = AakaClient(api_key=os.environ["AAKA_AGENT_KEY"])

out = client.pdf_compress("report.pdf", quality=75)
r   = client.pdf_extract("report.pdf")        # returns dict with "text", "pages", "metadata"
parts = client.pdf_split("book.pdf", "2s")    # returns list of output paths
out = client.pdf_merge(["a.pdf", "photo.jpg"])
out = client.pdf_delete_pages("report.pdf", "3-5")
```

---

## Mail Fetch (`/mail`, `m`)

Fetches POP3/IMAP accounts every 15 minutes via launchd. Stores messages locally in Maildir format. Keeps POP accounts alive by actively fetching.

**Chat commands (0 tokens, sensor-side):**

| Command | What it does |
|---------|-------------|
| `/mail` or `m` | List all accounts with message count and last fetch time |
| `m <account>` | List recent 10 messages (from, subject, date) |
| `m <account> 20` | List recent 20 messages |
| `m read <account> <N>` | Read message N (newest first) |
| `/mail fetch` | Trigger an on-demand fetch from all accounts |

**Setup (one-time, local):**
1. `python3 tools/pw_setup.py` — set master password in macOS Keychain
2. `python3 tools/pw_import.py /path/to/lastpass_export.csv` — import credentials
3. Edit `/Users/Shared/secrets/mail-fetch/accounts.yaml` — add your accounts
4. Install launchd: see `cli.md` → Mail Fetch section

**Passwords are never exposed in chat.** The fetch engine resolves credentials from the encrypted password store at runtime.

---

## Password Store (`/pw`)

Local encrypted password store. Imports LastPass CSV exports. Serves as the credential backend for mail fetch and other tools.

**Encryption:** AES-128 (Fernet) with PBKDF2-derived key. Master password stored in macOS Keychain. Safe to back up `vault.enc` — it's meaningless without the master password.

**Chat commands (0 tokens, sensor-side — no passwords in chat):**

| Command | What it does |
|---------|-------------|
| `/pw list` | List all entry names and usernames |
| `/pw list email` | Filter by group |
| `/pw search gmail` | Search by name, URL, or grouping |
| `/pw count` | Total number of entries |

**Write operations are local CLI only (passwords never transit chat):**
```bash
python3 tools/pw_setup.py                    # initial setup
python3 tools/pw_import.py export.csv        # import LastPass export
python3 tools/pw_import.py export.csv --overwrite  # update existing entries
```


---

## Channels

Aaka is Telegram-first; WhatsApp, Slack and Signal are add-on channels. Turn one
on with `ENABLED_CHANNELS` in `.env` (e.g. `ENABLED_CHANNELS=telegram,signal`).
Every intent in this manual works on every channel — the differences are only in
what each messenger itself can render.

| | Telegram | WhatsApp | Signal |
|---|---|---|---|
| Transport | Bot API long-poll | Baileys sidecar | signal-cli JSON-RPC daemon |
| Buttons (confirm / options) | inline keyboard | numbered reply | numbered reply |
| Files in + out | yes | yes | yes |
| Reactions (👀 read-ack) | yes | not wired | yes |
| Invite deep link | pre-filled `t.me` | pre-filled `wa.me` | `signal.me` + the line to send |

### Signal

**Use a dedicated number.** signal-cli can either *register* a number of its own
or *link* to your existing Signal account as a second device. Register a
dedicated number — a prepaid SIM is enough. Linking makes aaka **be** your
personal account: it sees every private conversation you have, and it can never
show up as its own contact in the family chat, which is how the bot is supposed
to work. `signal-cli link -n aaka` (scan the QR from Signal → Linked Devices) is
a fine five-minute test; it is not a way to run this.

Setup, in short — the full version is `INSTALL.md → Phase 5b`:

```bash
brew install signal-cli                       # Mac; VPS: apt install openjdk-21-jre-headless
signal-cli -a +15550000000 register           # the dedicated number
signal-cli -a +15550000000 verify 123456      # code from the SMS
```

```
# .env
ENABLED_CHANNELS=telegram,signal
SIGNAL_ACCOUNT=+15550000000
```

Then install the services and run `bash admin/diagnose.sh` — the **Signal** block
tells you whether the daemon, the poller and the family gate are all happy. The
daemon listens on `127.0.0.1:18794` and is never network-reachable.

**Where it runs matters.** Replies to *queued* intents (anything that needs the
executor) are sent by the outbox flusher, which runs from cron on the VPS. So
signal-cli belongs on the VPS too — that is the default,
`SIGNAL_PLACEMENT=sensor`. With `SIGNAL_PLACEMENT=executor` (daemon on the Mac)
instant replies work but queued ones stay pending, and the flusher logs that it
skipped them.

**Being let in.** aaka only answers people it knows. Either add a member's number
to `aaka.yaml` (`signal: "+491700000000"`) or run `/invite <name>` and send them
the link. An unknown sender always gets their own handle back with "share it with
whoever set me up" — never silence. Set `SIGNAL_GROUP_ID` (find it with
signal-cli's `listGroups`) to let a whole family group in.

**No buttons.** Where Telegram shows inline buttons, Signal gets a numbered list:

```
Add "physio" Fri 15:00–16:00?

Reply with a number:
1. Yes
2. Change time
3. Cancel
```

Reply `2` and aaka treats it exactly as a button tap. The numbering expires after
6 hours and each list answers once.

**Note to self.** If aaka's number is in your own contacts you can message it
directly. aaka also reads your Signal *note-to-self* when it is running on that
same account, but it deliberately ignores copies of messages you send to anyone
else — it will not join conversations it wasn't addressed in.

> Status: the Signal channel is implemented and unit-tested against a mock
> signal-cli daemon. It has **not yet been verified against a live Signal
> account**.
