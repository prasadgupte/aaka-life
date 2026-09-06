# Aaka demo chat

A Telegram-clone chat panel for Aaka. Two delivery modes:

- **Live mode** (`live.html` + `server.py`) — a real channel. Type messages,
  the in-process sensor routes them, the reply comes back. Pick which family
  member is sending. Select any subset of the conversation and save it as a
  cassette with one click. Runs only on the executor (your Mac, localhost).
- **Cassette player** (`index.html`) — static HTML that animates a recorded
  cassette JSON. No backend, opens via `file://`, embeddable anywhere. This
  is the marketing-site artifact.

Both share the same Telegram-faithful CSS.

---

## Run the live server

```bash
# Demo mode (Ash-Kaa sample family, sample DB):
venv/bin/python3 \
    executor/webui/server.py --config samples/demo --port 18791

# Live mode (your real config):
venv/bin/python3 \
    executor/webui/server.py --config /Users/Shared/aaka-repo-config --port 18791

# Then:
open "http://localhost:18791/static/live.html"
```

Mode is chosen at startup (the sensor reads `AAKA_CONFIG_DIR` at module
import time). Restart the server to switch modes — the UI shows which is
active in the header chip.

### Inside live mode

- **Member chip** (header) — click to pick which family member is the sender.
  Only members with a `telegram` id in `aaka.yaml` are selectable (the
  sensor rejects unknown senders).
- **＋ button** (header) — toggle selection mode. Click any message bubble
  to mark it. The footer bar shows the count + "💾 Save cassette".
- **Save cassette** — type a title, optionally a summary, click Save. The
  cassette JSON lands in `cassettes/<slug>.json` with `ts_offset_ms`
  rebased to the first selected message; the manifest is updated; the
  toast prints the play URL.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/healthz` | liveness |
| `GET`  | `/webui/state` | bot, members, mode, fresh `session_id` |
| `POST` | `/webui/messages` | body `{text, member_id, session_id}` → `{reply, ts_ms, latency_ms}` |
| `POST` | `/webui/upload` | multipart `{file, member_id, session_id, text?}` → `{reply, filename, size_bytes, staged_path, …}` |
| `GET`  | `/webui/stream?session=<sid>` | SSE outbound — pushes `outbox_items` where `source='web' AND channel_id='web:<sid>'` |
| `POST` | `/webui/cassettes` | save `{title, summary?, messages}` |
| `GET`  | `/webui/cassettes` | manifest contents |
| `GET`  | `/cassettes/{name}.json` | serve a cassette for the static player |
| `GET`  | `/static/*` | static files (live.html, index.html, …) |

### Bidirectional flow

Every inbound web message is wrapped in a sensor "Format A" header with
`channel="web"` and `channel_id="web:<session_id>"`. This `source="web"` tag
flows through:

```
sensor.route() → queue_items.source='web' → executor → outbox_items.source='web'
                                                                 ↓
                                          GET /webui/stream picks it up via SSE
```

**Strict routing guards** (so web messages never leak to Telegram/WhatsApp):
- `sensor/flush_outbox.py` skips `source='web'` rows entirely.
- `executor/vps_sync.py:_push_outbox` excludes `source='web'` from the
  Mac→VPS sync. Web rows stay Mac-local.

### What works

- Every **sensor-direct** intent (`/today`, `/week`, `/tasks`, `/notes`,
  `/buy`, `/menu`, `/engage`, `/bday`, `/pdf`, `/status`, …).
- **File uploads.** Click 📎, pick a file, optionally type a caption like
  `f #school permission slip`. The file is staged under
  `$AAKA_CONFIG_DIR/data/staging/web-<uuid>/` and the sensor's `drop_file`
  intent runs end-to-end.
- **Queue-based intents** (`/add`, `/addtask`, `/block`) in **live mode**:
  enqueue, the executor (`com.aaka.queueworker` launchd daemon) processes,
  the completion reply lands in the browser via SSE. In **demo mode**: the
  inline preview appears but completion won't — no demo executor in v2.

### v2 still doesn't do

- **Inline keyboard buttons** (`reply_markup` JSON). For now, type
  `/confirm #<hash>` to manually confirm a queued action.
- **Demo-mode executor.** Queue intents enqueue but don't complete in demo.
- **Auth.** Localhost only. Don't expose port 18791 publicly.

---

## Play a cassette (no backend)

The static player is just HTML — open it directly from disk:

```bash
open executor/webui/static/index.html?play=tour\&autoplay=1
```

…or serve the directory over HTTP if your browser blocks `fetch` on
`file://` (most Chromium-based browsers do):

```bash
python3 -m http.server 8089 --directory executor/webui
open "http://localhost:8089/static/index.html?play=tour&autoplay=1"
```

URL parameters:

| Param | Default | Notes |
|---|---|---|
| `play=<name>` | `tour` | Loads `cassettes/<name>.json` |
| `autoplay=0\|1` | `1` | When `0`, all turns render at once (no animation) |
| `speed=1.5` | `1` | Playback speed multiplier (useful for video edits) |

Seed cassettes shipped: see `cassettes/manifest.json`.

## Record a new cassette

The recorder drives the real sensor in-process against the bundled
`samples/demo/` config. Every reply you see is the actual Aaka output.

```bash
AAKA_CONFIG_DIR=$(pwd)/samples/demo \
    python3 executor/webui/record_cassette.py
```

Inside the recorder:

```
▸ Title: Morning routine
▸ Summary (one line): The first ten minutes of the day with Aaka.
▸ Starting member [alex]: alex

alex> /today
🌤️ Aaka: 📅🚗 On call for Kiran. Here's the plan…

alex> :tsu /tasks                 # switch sender to tsu
tsu> @/path/to/file.jpg f #school permission slip
                                  # @path attaches a file
tsu> /done                        # save and exit
```

Output lands in `cassettes/<slug>.json`; the manifest is auto-updated.

Editing tip: open the cassette JSON afterwards to tighten `ts_offset_ms`
gaps or trim a long bot reply for video purposes. The format is plain
enough to hand-edit.

## What works in cassettes

v0 covers **sensor-direct intents** — every command that returns its reply
immediately without queueing:

- `/today`, `/week`, `/day Mon`, `/tasks`, `/tasks #tag`, `/notes <tag>`,
  `/buy <list>`, `/menu`, `/engage`, `/bday`, `/pdf …`, `/status`, `/tags`

Out of v0 (and out of cassettes for now):

- `/add`, `/addtask`, `/block` — queue-based, need executor in the loop.
- Inline Yes/No confirmations.
- Real file uploads through the chat (the recorder accepts `@<path>` but
  doesn't actually send the file through the sensor; the cassette stores
  a chip that *looks* like an attachment).

## Layout

```
executor/webui/
├── static/
│   ├── index.html       Chat shell. Reads ?play=&autoplay= from URL.
│   ├── chat.css         Telegram-clone dark theme + bubble layout.
│   ├── chat.js          Cassette loader + animator.
│   └── md.js            Minimal Markdown → safe HTML.
├── cassettes/
│   ├── manifest.json    List of available cassettes (name, title, summary).
│   ├── tour.json        Seed.
│   ├── morning.json     Seed.
│   └── assets/          Thumbnails referenced by cassettes.
└── record_cassette.py   Dev-only authoring tool (not shipped to adopters).
```

## License

PolyForm Noncommercial 1.0.0 — same as the rest of the repo.
