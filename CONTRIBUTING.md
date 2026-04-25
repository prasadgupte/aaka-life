# Contributing to Aaka

## Getting started

```bash
git clone https://github.com/prasadgupte/aaka-life.git
cd aaka-life
python3 -m venv venv
venv/bin/pip install -r requirements.txt -q
git config core.hooksPath .githooks
```

The pre-commit hook in `.githooks/pre-commit` blocks commits that contain private data (real names, phone numbers, API keys, VPS IPs). Read the hook comments if it blocks you unexpectedly.

## Running the test suite

```bash
bash admin/test.sh
```

Tests run against real local config when `AAKA_CONFIG_DIR` is set. They skip gracefully when config isn't present.

## Code layout

| Path | What goes here |
|------|---------------|
| `skills/` | One module per intent. Add `handle(payload)` → returns string reply. |
| `sensor/router_sensor.py` | Intent routing. Add a new `elif intent == "..."` block. |
| `executor/queue_worker.py` | Home-side handlers that need OAuth (calendar writes, etc.) |
| `gateway/` | LLM + messaging adapters — do not add skill logic here |
| `admin/` | Ops scripts (diagnose, deploy, test). Append, don't rewrite. |

## Adding a new intent

1. Create `skills/<category>/<intent>.py` with a `handle(payload) -> str` function.
2. Add a `SKILL_META` dict at the top: `{"name": ..., "description": ..., "requires": [...]}`.
3. Register it in `skills/registry.yaml`.
4. Add a routing case in `sensor/router_sensor.py` (keep the pattern consistent with neighbours).
5. Update `CLAUDE.md` Intents table, `docs/manual.md`, the `/menu` block in `router_sensor.py`, and `admin/test.sh`.

## Privacy rules

- No real names, phone numbers, or IDs in code or test fixtures. Use `alice`/`bob`/`charlie`.
- No VPS IPs (use `$AAKA_VPS_IP`), no OAuth tokens, no API keys.
- The pre-commit hook enforces these patterns automatically.

## PR guidelines

- Keep PRs focused. One intent or one fix per PR.
- New skills need a test in `admin/test.sh`.
- Update `CLAUDE.md` when adding user-visible features (the Intents table is the source of truth).
