# Aaka Agent API

Quick-start for external agents (travel planner, research, etc.) that need to
send messages, fetch emails, create todos, or interact with the user through
aaka's infrastructure.

---

## Two integration paths

### Path A: Gateway HTTP API

For remote or isolated agents — runs over HTTP, requires registration.

- **Requires:** gateway running (`localhost:18790`) + registered agent key
- **SDK:** `gateway/agent_client.py` → `AakaClient`
- **Good for:** agents on VPS, in Docker, in git worktrees, or any process that shouldn't import aaka internals

### Path B: Direct Python imports

For co-located agents on the same Mac — no gateway, no registration.

- **Requires:** `sys.path.insert(0, "/path/to/aaka-repo")` (the repo root on your Mac)
- **No gateway, no API key, no registration**
- **Good for:** Claude Code agents, browser-harness scripts, cron jobs on Mac

| Module | Import | Key functions |
|--------|--------|---------------|
| Gmail | `from skills.mail.gmail import ...` | `get_gmail_service`, `list_by_label`, `fetch_full` |
| Todos | `from skills.tasks.local_tasks import ...` | `add_task`, `list_open`, `complete_task_by_id`, `format_tasks` |

**When to use which:**
- Path B is simpler and works immediately. Use it when the agent runs on the same Mac as the aaka config dir.
- Path A is required when the agent is remote, or when you want audit trails (agent registry tracks `last_used_at`).

---

## Registering an agent (Path A only)

```bash
# Register — prints API key once (not stored, only SHA-256 hash kept)
python3 admin/register_agent.py <agent-id> "<Display Name>"

# List all agents
python3 admin/register_agent.py --list

# Revoke an agent
python3 admin/register_agent.py --revoke <agent-id>
```

After registering, set the key in the agent's environment:

```bash
export AAKA_AGENT_KEY='aaka-...'
```

**Standing rule:** Update the "Registered Agents" table in `/Users/Shared/aaka-repo/CLAUDE.md` after every registration.

---

## Gateway endpoints (Path A)

Base URL: `http://localhost:18790`

| Method | Endpoint | Purpose | Auth |
|--------|----------|---------|------|
| `POST` | `/v1/messages` | Send message (fire-and-forget or await reply) | `X-Agent-Key` |
| `POST` | `/v1/photos` | Send a photo/image to the user | `X-Agent-Key` |
| `POST` | `/v1/approvals` | Queue a LinkedIn post for human approval (+ optional schedule) | `X-Agent-Key` |
| `GET` | `/v1/approvals/{id}` | Poll approval status | `X-Agent-Key` |
| `GET` | `/v1/replies/{correlation_id}` | Poll for user reply (404 if none yet) | `X-Agent-Key` |
| `DELETE` | `/v1/replies/{correlation_id}` | Mark reply as consumed | `X-Agent-Key` |
| `POST` | `/v1/calendar/events` | Add event(s) to Google Calendar | `X-Agent-Key` |
| `POST` | `/v1/tasks` | Add a task to local store | `X-Agent-Key` |
| `GET` | `/v1/tasks?project=X&skill=Y&title_prefix=Z` | List tasks for this agent | `X-Agent-Key` |
| `GET` | `/v1/gmail/emails?label=X&member_id=Y` | Fetch emails by Gmail label | `X-Agent-Key` |
| `POST` | `/v1/gmail/labels` | Create Gmail label | `X-Agent-Key` |
| `POST` | `/v1/gmail/drafts` | Create a Gmail draft (admin account) | `X-Agent-Key` |
| `POST` | `/v1/email/send` | Queue email (immediate or scheduled) via VPS sensor | `X-Agent-Key` |
| `POST` | `/v1/scheduled/messages` | Schedule any outbound (email/telegram/whatsapp) | `X-Agent-Key` |
| `GET` | `/v1/scheduled/messages/{id}` | Poll scheduled message status | `X-Agent-Key` |
| `GET` | `/v1/scheduled/messages?status=X` | List agent's scheduled messages | `X-Agent-Key` |
| `DELETE` | `/v1/scheduled/messages/{id}` | Cancel a pending scheduled message | `X-Agent-Key` |
| `POST` | `/v1/jobs` | Create/update a recurring scheduled job | `X-Agent-Key` |
| `GET` | `/v1/jobs` | List agent's jobs | `X-Agent-Key` |
| `PATCH` | `/v1/jobs/{id}` | Update schedule/payload/enabled | `X-Agent-Key` |
| `DELETE` | `/v1/jobs/{id}` | Remove a job | `X-Agent-Key` |
| `POST` | `/v1/llm` | One-shot LLM call (Claude CLI, Gemini fallback); optional images | `X-Agent-Key` |
| `GET` | `/health` | Health check (no auth) | None |

### Starting the gateway

```bash
# Via launchd (auto-start on Mac login)
launchctl load ~/Library/LaunchAgents/com.aaka.agentapi.plist

# Manual
cd /path/to/aaka-repo && python3 gateway/agent_api.py

# Verify
curl -s http://localhost:18790/health  # → {"status":"ok"}
```

---

## SDK quick-start (Path A)

```python
import os
from gateway.agent_client import AakaClient

client = AakaClient(api_key=os.environ["AAKA_AGENT_KEY"])

# Fire-and-forget message to user
client.send("Flight booked! LH179 BER→FRA, May 18.")

# Blocking ask — polls until user replies or timeout
reply = client.ask(
    "Window or aisle?",
    options=["Window", "Aisle"],
    timeout_minutes=60,
)
# reply == "Window"

# Add a calendar event (silent Telegram notification sent automatically)
client.add_event("Flight LH179 BER→FRA", "2026-05-18",
                 start_time="14:30", end_time="16:00",
                 description="Seat 12A, booking ref XYZABC")

# Add a recurring event with multiple occurrences
client.add_event("Piano lesson", "2026-05-10", occurrences=[
    {"date": "2026-05-10", "start_time": "16:00", "end_time": "17:00"},
    {"date": "2026-05-17", "start_time": "16:00", "end_time": "17:00"},
])

# Add a task (silent Telegram notification sent automatically)
client.add_task("Print boarding passes for LH179",
                owner="alex", due_date="2026-05-17",
                tags=["#travel"])

# Fetch emails by Gmail label
emails = client.gmail_emails("Travel/2605 London", member_id="alex")
for e in emails:
    print(e["subject"], e["date"])

# Create a Gmail label
client.gmail_create_label("Travel/2607-Japan-China-f", member_id="alex")

# Create a Gmail draft (lands in Drafts folder; review + send in Gmail)
draft = client.gmail_create_draft(
    to="recruiter@company.com",
    subject="Intro: Alex Smith – VP Product",
    body="Hi,\n\nI wanted to reach out...",
    cc="alex@work.example.com",            # optional
)
# draft = {"draft_id": "...", "message_id": "...", "thread_id": "..."}
```

**SDK methods:**

| Method | Returns | Blocking? |
|--------|---------|-----------|
| `send(text, ...)` | `outbox_item_id` | No |
| `send_photo(path, caption, reply_options, ...)` | `{"ok": True, "message_id": int}` | No |
| `ask(text, options, timeout_minutes, ...)` | reply text | Yes (polls) |
| `add_event(title, date, start_time, ...)` | `dict` (event_ids, count) | No |
| `add_task(title, owner, due_date, project, skill, ...)` | `dict` (task_id, title, due_date, agent, project, skill) | No |
| `gmail_emails(label, member_id, max_results)` | `list[dict]` | No |
| `gmail_create_label(name, member_id)` | `dict` (id, name, type) | No |
| `gmail_create_draft(to, subject, body, cc, bcc, html_body, reply_to_message_id)` | `dict` (draft_id, message_id, thread_id) | No |
| `send_email(to_member_id, subject, body, schedule_at, ...)` | `dict` (id, status, scheduled_at_utc, to) | No |
| `schedule_outbound(channel, payload, schedule_at)` | `dict` (id, status, scheduled_at_utc, channel) | No |
| `get_scheduled_message(id)` | `dict` (full row) | No |
| `list_scheduled_messages(status)` | `list[dict]` | No |
| `cancel_scheduled_message(id)` | None | No |
| `schedule_job(name, schedule, payload)` | `dict` (job) | No |
| `list_jobs()` | `list[dict]` | No |
| `delete_job(job_id)` | None | No |
| `post_linkedin(text, image_path, schedule_at, visibility)` | `dict` (approval) | No |
| `get_approval_status(approval_id)` | `dict` (status) | No |

### Scheduled outbound — email, Telegram, WhatsApp

Send to any channel at a specific future time. Delivered by the always-on VPS sensor (`sensor/scheduled_sender.py`, every 60s) — fires even when Mac is asleep.

**`schedule_at` rules:**
- Must be ISO 8601 with an explicit timezone offset — `"2026-06-01T09:00:00+02:00"` or `"...Z"`.
- Naive datetimes (no TZ) are rejected with 422.
- Omit or pass `None` for near-immediate delivery (~60s via VPS cron).
- Max 90 days in the future; not more than 5 min in the past.

```python
# Email — to a registered family member (must have email in aaka.yaml)
r = client.send_email(
    "alex",                                    # to_member_id
    "Flight confirmed",                        # subject
    "Departs 14:30 BER. Gate B22.",            # plain-text body
    html_body="<b>Departs 14:30</b> BER.",    # optional HTML
    schedule_at="2026-06-01T09:00:00+02:00",  # omit for ~immediate
)
# r → {"id": "...", "status": "pending", "scheduled_at_utc": "...", "to": "..."}

# Poll until sent
import time
while True:
    msg = client.get_scheduled_message(r["id"])
    if msg["status"] in ("sent", "error"):
        print(msg["status"], msg.get("result"), msg.get("error"))
        break
    time.sleep(15)

# Generic — any channel
client.schedule_outbound(
    "telegram",
    {"channel_id": "12345678", "text": "Reminder: meeting in 15 min", "silent": False},
    schedule_at="2026-06-01T09:45:00+02:00",
)
client.schedule_outbound(
    "whatsapp",
    {"channel_id": "+49151...", "text": "Your booking is confirmed."},
    schedule_at="2026-06-01T08:00:00+02:00",
)

# Manage
msgs = client.list_scheduled_messages(status="pending")   # all pending for this agent
client.cancel_scheduled_message(r["id"])                  # only if still pending
```

**Payload shapes by channel:**

| Channel | Required fields | Optional fields |
|---------|----------------|-----------------|
| `email` | `to_member_id`, `subject`, `body` | `html_body`, `from_member_id` (default `"aakash"`) |
| `telegram` | `channel_id` or `recipient`, `text` | `silent` (bool), `reply_markup` (dict) |
| `whatsapp` | `channel_id` or `recipient`, `text` | — |

**Status lifecycle:** `pending` → `sent` (with `result.message_id`) or `error` (with `error` string). No automatic retries — poll and decide if retry is needed.

**Email recipients:** only registered family members with a non-empty `email` in `aaka.yaml`. Others return 422.

---

### Job scheduler

Schedule recurring tasks without managing launchd/cron yourself:

```python
# Cron expression (5-field standard)
client.schedule_job("fetch-travel-emails", "*/10 * * * *",
    payload={"label": "Travel/AAKA-JP", "member_id": "alex"})

# Interval shorthand: "15m", "1h", "6h", "1d"
client.schedule_job("daily-digest", "1d", payload={"type": "summary"})

# Upserts by (agent_id, name) — safe to call on every session start
# List and manage
jobs = client.list_jobs()
client.delete_job(jobs[0]["id"])
```

The gateway fires due jobs into the queue as `intent=agent_job` every 30 seconds.
Job payload is passed through to the executor for dispatch.

### Calendar events

Add events directly to Google Calendar. A silent Telegram notification is sent automatically.

```python
# Single timed event
client.add_event("Dentist appointment", "2026-06-01",
                 start_time="10:00", end_time="11:00",
                 member_id="alex")

# All-day event
client.add_event("Public holiday", "2026-12-25")

# Multi-occurrence (e.g. weekly class)
client.add_event("Japanese class", "2026-05-10", occurrences=[
    {"date": "2026-05-10", "start_time": "18:00", "end_time": "19:30"},
    {"date": "2026-05-17", "start_time": "18:00", "end_time": "19:30"},
    {"date": "2026-05-24", "start_time": "18:00", "end_time": "19:30"},
])

# With guests and location
client.add_event("Team dinner", "2026-05-20",
                 start_time="19:00", end_time="21:00",
                 location="Kin Dee, Berlin",
                 guests=["alice@example.com"],
                 description="Reservation for 4")
```

**Event payload fields:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `title` | str | Yes | Event title |
| `date` | str | Yes | `YYYY-MM-DD` (first/only occurrence) |
| `start_time` | str | No | `HH:MM` 24h — omit for all-day |
| `end_time` | str | No | `HH:MM` 24h |
| `end_date` | str | No | `YYYY-MM-DD` for multi-day events |
| `occurrences` | list | No | Override single date; each: `{date, start_time?, end_time?}` |
| `description` | str | No | Event description/notes |
| `location` | str | No | Event location |
| `member_id` | str | No | Whose calendar (resolves calendar_id) |
| `calendar_tag` | str | No | e.g. `"kids"` — overrides member resolution |
| `guests` | list | No | Email addresses to invite |
| `private` | bool | No | Mark event as private |
| `timezone` | str | No | IANA timezone (default: from config) |

### Tasks

Add tasks to the local store. A silent Telegram notification is sent automatically.

```python
client.add_task("Book restaurant for team dinner",
                owner="alex",
                due_date="2026-05-19",
                tags=["#travel", "#food"],
                project="japan26",
                skill="restaurant_search")
```

**Task payload fields:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `title` | str | Yes | Short action line |
| `owner` | str | No | `"alex"`, `"tsu"`, etc. |
| `due_date` | str | No | `YYYY-MM-DD` |
| `urgent` | bool | No | Sets priority to high |
| `starred` | bool | No | Sets priority to medium |
| `tags` | list | No | e.g. `["#travel"]` — agent tag auto-added |
| `description` | str | No | Longer details |
| `recurring` | bool | No | Recurring task |
| `project` | str | No | Entity/project slug for traceability, e.g. `"japan26"`, `"flo172"` |
| `skill` | str | No | Skill path that created this task, e.g. `"book_flight"` |

**Provenance fields (auto-set, read-only):**

| Field | Value | Notes |
|-------|-------|-------|
| `agent` | authenticated agent id | Auto-set from API key — `"travel"`, `"coach"`, etc. |

Tasks created by the user directly through Aaka (Telegram/WhatsApp) have `agent="aaka"`. Tasks created via the API have `agent` set to the calling agent's registered id. An `#agent:<agent-id>` tag is also appended for easy filtering.

**Response fields:** `task_id`, `title`, `due_date`, `action_id`, `project`, `agent`, `skill`.

#### Listing tasks (dedup / upsert pattern)

Use `GET /v1/tasks` to check what already exists before creating duplicates.
Results are always scoped to the calling agent — other agents' tasks are never returned.

```python
# Check before creating (upsert-by-title)
existing = client.list_tasks(
    project="2607-Japan-China-f",
    skill="weather",
    title_prefix="Pack: ",
)
existing_titles = {t["title"] for t in existing}
for item in packing_list:
    title = f"Pack: {item}"
    if title not in existing_titles:
        client.add_task(title, project="2607-Japan-China-f", skill="weather")
```

**Query parameters (all optional):**

| Param | Default | Notes |
|-------|---------|-------|
| `project` | — | Exact match on `project` field |
| `skill` | — | Exact match on `skill` field |
| `owner` | — | Exact match on `owner` field |
| `status` | `open` | `open` \| `done` \| `deleted` \| `all` |
| `tag` | — | Match if value is in `tags` array; repeatable (`?tag=a&tag=b`) |
| `title_prefix` | — | Case-sensitive prefix match on title |
| `limit` | `200` | Max rows returned; hard cap `1000` |

**Response:** `{"tasks": [{id, title, owner, due_date, urgent, starred, tags, description, recurring, project, skill, agent, status, created_at, updated_at}], "count": N}`

### Silent notifications

Both `add_event()` and `add_task()` send a **silent** Telegram notification to the user automatically. The notification includes the agent name and what was created. Silent means the phone won't buzz — the user sees it next time they open the chat.

### Photos

Send an image to the user via Telegram's `sendPhoto` API:

```python
# Send image only
client.send_photo("draft.png", caption="*Post draft*\n\nHere's your content.")

# Send image with inline decision buttons
client.send_photo(
    "draft.png",
    caption="*Draft: Your next post*\n\n...",
    reply_options=["Approve", "Edit", "Reject"],
)

# Note: reply_options on send_photo does NOT block waiting for a reply.
# Follow it with client.ask() if you need to collect the decision.
```

**`POST /v1/photos` request body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `photo_b64` | str | yes | base64-encoded image bytes |
| `filename` | str | no | original filename; used for MIME detection (default: `photo.jpg`) |
| `caption` | str | no | Markdown caption (max 1024 chars) |
| `sender` | str | no | Telegram chat ID override |
| `silent` | bool | no | send without notification sound |
| `reply_options` | list[str] | no | inline keyboard buttons |

### LLM calls — text, or text + images

```python
# Text only — unchanged
data = client.llm('Extract {name, date} from: "Dinner May 10"')            # JSON string
summary = client.llm("Summarize: ...", response_format="text", complexity="medium")

# With images — refer to them as "Image 1", "Image 2 (label)" in the prompt
r = client.llm(
    "Explain the question in Image 1 to a ten-year-old. The answer options are pictures, "
    "see Image 2.",
    images=[client.llm_image("q14.png", label="the question"),
            client.llm_image("q14-options.png", label="the options")],
    response_format="text", complexity="high", raw=True,
)
r["text"]; r["saw_images"]    # saw_images < len(images) → not (fully) vision-backed
```

**`POST /v1/llm` request body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `prompt` | str | yes | |
| `response_format` | `json` \| `text` | no | default `json` (prepends a "JSON only" instruction) |
| `complexity` | `low` \| `medium` \| `high` | no | haiku / sonnet / opus on the Claude path; default `low` |
| `images` | list | no | ≤ 4 of `{data: base64 (no data: prefix), media_type: image/png \| image/jpeg, label: str}`; each ≤ 2 MB decoded, ≤ 8 MB total |

**Response:** `{text, model, provider, duration_ms, saw_images}`.

How images travel: on the Claude-CLI path each one is written to a private temp
dir, the prompt gets a line per image (`Image 1 (the question): /…/1.png`), and
`claude -p` runs with only the `Read` tool, allowed only inside that dir; the
dir is deleted afterwards. `saw_images` counts the images the model actually
read (a denied or failed Read does not count). If it read none, the call falls
through to Gemini, which takes the images inline (`saw_images` = all). Nothing
ever returns a text-only answer while claiming to have seen the picture.

| Status | Body | When |
|--------|------|------|
| 413 | `{error: too_many_images \| image_too_large \| images_too_large, …}` | count or size cap hit |
| 422 | `{error: bad_image, index, reason}` | base64 does not decode |
| 422 | `{error: vision_unsupported, provider}` | the fallback provider cannot take images |
| 502 | `LLM call failed: …` | both providers failed |

Each call appends `{images, saw_images}` to `logs/llm-usage.jsonl` when images were sent.

### LinkedIn posts

Post to LinkedIn with a human-in-the-loop approval step. The gateway sends a Telegram preview with **Post ✅ / Cancel ❌** buttons. The post is published (or scheduled on LinkedIn natively) only after the user approves.

```python
# Post now — sends preview, publishes on approval
r = client.post_linkedin("Excited to share our latest thinking on...")
print(r["approval_id"])   # poll this for status

# Schedule for a future UTC datetime — sends preview, schedules on LinkedIn on approval
r = client.post_linkedin(
    "Weekly update...\n\n#product #leadership",
    schedule_at="2026-05-19T09:00:00Z",
)

# With an image
r = client.post_linkedin(
    "Announcing our new feature...",
    image_path="/tmp/feature-announce.png",
    schedule_at="2026-05-20T08:00:00Z",
)

# Poll for outcome
status = client.get_approval_status(r["approval_id"])
# status["status"] in: awaiting_confirm | scheduled | confirmed | done | cancelled | error
```

**`post_linkedin()` fields:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `text` | str | Yes | Post body |
| `image_path` | str | No | Local path to JPEG/PNG |
| `schedule_at` | str | No | ISO 8601 UTC — e.g. `"2026-05-19T09:00:00Z"`. Omit to publish immediately on approval. |
| `visibility` | str | No | `"PUBLIC"` (default) or `"CONNECTIONS"` |

**Status values:**

| Status | Meaning |
|--------|---------|
| `awaiting_confirm` | Preview sent, waiting for user to tap Approve or Cancel |
| `scheduled` | Approved and queued for future execution |
| `confirmed` | Approved and queued for immediate execution |
| `done` | Published (or scheduled on LinkedIn) |
| `cancelled` | User tapped Cancel |
| `error` | Execution failed — check `result.error` |

### Multi-turn approval flows

`ask()` blocks until the user replies, so sequential calls work naturally for multi-turn flows. Each call uses its own `correlation_id` — as long as you don't call `ask()` concurrently for the same sender, there are no conflicts.

**Important:** Multi-turn flows need more than the 300-second subprocess timeout used by `_exec_agent_job`. Run the agent as a standalone long-lived script instead of scheduling it via `schedule_job()`.

```python
# Pattern for content approval with Edit branch
for post in posts:
    if post.get("image_path"):
        client.send_photo(post["image_path"], caption=f"*{post['title']}*\n\n{post['body']}")
    else:
        client.send(f"*{post['title']}*\n\n{post['body']}")

    choice = client.ask(
        "What would you like to do?",
        options=["Approve", "Edit", "Reject", "Skip"],
        timeout_minutes=60,
    )

    if choice == "Approve":
        publish(post)
        client.send(f"Published: _{post['title']}_", silent=True)
    elif choice == "Edit":
        feedback = client.ask("What changes?", timeout_minutes=60)
        apply_edits(post, feedback)
        client.send(f"Updated _{post['title']}_ with your feedback.", silent=True)
    elif choice == "Reject":
        reject(post)

client.send("Review complete.")  # fire-and-forget confirmation
```

See `examples/content_approval.py` for the full runnable example.

---

## Direct import quick-start (Path B)

### Gmail — read emails

```python
import sys
sys.path.insert(0, "/path/to/aaka-repo")
from skills.mail.gmail import get_gmail_service, list_by_label, fetch_full

svc = get_gmail_service(member_id="alex")
msgs = list_by_label(svc, "Travel/2605 London", max_results=10)

for m in msgs:
    full = fetch_full(svc, m["id"], max_chars=5000)
    print(f"Subject: {full['subject']}")
    print(f"From: {full['from_addr']}")
    print(f"Date: {full['date']}")
    print(full["body"][:2000])
```

### Todos — create and list

```python
import sys
sys.path.insert(0, "/path/to/aaka-repo")
from skills.tasks.local_tasks import add_task, list_open, format_tasks

# Create a task
task = add_task({
    "title": "Add 2605-LHR-w flights to OpenFlights tracker",
    "owner": "alex",
    "due_date": "2026-05-22",
    "urgent": False,
    "starred": False,
    "tags": ["#travel"],
    "recurring": False,
    "description": "4 legs: BER-FRA, FRA-LHR, LHR-MUC, MUC-BER. Ref AO6FOJ.",
})
print(f"Created: {task['id']} — {task['title']}")

# List open tasks for a person
tasks = list_open("alex")
print(format_tasks(tasks))

# Filter by tag
travel_tasks = [t for t in list_open("alex") if "#travel" in t.get("tags", [])]
```

**Task payload fields:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `title` | str | Yes | Short action line |
| `owner` | str | No | `"alex"`, `"tsu"`, etc. |
| `due_date` | str | No | `YYYY-MM-DD` |
| `urgent` | bool | No | Sets priority to `high` (🔴) |
| `starred` | bool | No | Sets priority to `medium` (⭐) |
| `tags` | list | No | e.g. `["#travel", "#work"]` |
| `recurring` | bool | No | |
| `description` | str | No | Longer details |

**Task management:**

| Function | Purpose |
|----------|---------|
| `add_task(payload)` | Create → returns task dict |
| `list_open(owner="")` | List visible open tasks |
| `complete_task_by_id(id)` | Mark done |
| `complete_task_by_num(n)` | Mark done by display number |
| `snooze_task(id, days)` | Snooze N days |
| `find_by_title(query)` | Search by substring |
| `format_tasks(tasks)` | Pretty-print for messaging |

**Storage:** `$AAKA_CONFIG_DIR/data/tasks/tasks.json` — auto-created on first write.

### Calendar (read-only)

Pre-rendered schedule files live in `$AAKA_CONFIG_DIR/data/calendar/`:
- `today.md` — today's schedule
- `weekly.md` — this week's schedule

Updated by the executor cron. Read-only from agent perspective.

---

## Auth & scopes (Gmail)

| Scope | Enables | Current status |
|-------|---------|----------------|
| `gmail.readonly` | `list_by_label`, `fetch_full` | Active (all member tokens) |
| `gmail.labels` | `create_label` | Active (see aaka.yaml for which members) |
| `gmail.modify` | readonly + labels + mark read + **create drafts** | Active (see aaka.yaml) |
| `gmail.send` | `send_email` via scheduled outbound | Active — aakash token (Mac + VPS) |

Token location: `$AAKA_CONFIG_DIR/auth/` (resolved via `aaka_config.auth_for(member_id)`).

To add scopes:
```bash
# Update OAuth scopes in config, then re-authorize
python3 admin/reauth.py alex
```

---

## Prerequisites checklist

| Requirement | Path A (gateway) | Path B (direct) |
|-------------|-------------------|-----------------|
| Gateway running | Yes | No |
| Agent registered | Yes | No |
| API key in env | Yes | No |
| Python 3.10+ | Yes | Yes |
| `aaka_config.py` reachable | Via gateway | Via `sys.path` |
| Gmail token on disk | Via gateway | Yes |
| Tasks JSON writable | Via gateway (future) | Yes |

---

## Source files

| File | Purpose |
|------|---------|
| `gateway/agent_api.py` | FastAPI gateway (endpoints, auth) |
| `gateway/agent_client.py` | Python SDK (`AakaClient`) |
| `admin/register_agent.py` | Agent registration CLI |
| `skills/mail/gmail.py` | Gmail read/label operations |
| `skills/tasks/local_tasks.py` | JSON task store CRUD |
| `aaka_config.py` | Config/path resolution |
| `aaka_queue/schema.sql` | DB schema (agent_registry, agent_replies, scheduled_messages tables) |
| `sensor/scheduled_sender.py` | VPS cron — fires due scheduled_messages (email/telegram/whatsapp) |
| `executor/vps_sync.py` | Mac↔VPS sync including scheduled_messages table |
