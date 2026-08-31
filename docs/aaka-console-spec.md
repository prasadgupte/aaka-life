# Aaka Console — lightweight local UI (spec)

**Why:** Removing OpenClaw removes the gateway UI it gave us for free. Aaka Console fills that
gap — a single lightweight, localhost-only web surface bundled with aaka for people who don't
live in the CLI, and a place the setup flow can point to. Reinforces "your infra, no middleware."

**Not greenfield.** ~80% exists — this is integration + reskin + one new view:
| Surface | Source | Work |
|---|---|---|
| **Console** (try commands) | `executor/webui/` — real `channel="web"` chat, SSE stream, uploads | Reskin to the aaka brand + make it *look like* the aaka.life widget (suggested commands, tap-to-send, streamed replies) |
| **Tasks** | `taskboard/` — existing board + API | Reskin to brand; embed as a tab |
| **Status & logs** | NEW (small) | `setup_check --json` as a **capability board** (ties to onboarding-redesign) + tails of `telegram_poller` / queue logs. Read-only. |
| **Design system** | `aaka-site/brand/` (tokens/components/global CSS) | Vendor into `webui/brand/` as the single shared source — CSS-only, no build step |

## Architecture
- **One Python server** (extend `executor/webui/server.py`) serves a unified shell with three
  routes/tabs: `Console · Tasks · Status`. Shared brand CSS across all three.
- **localhost only, no auth** (local surface). Runs via a launchd plist like taskboard's, on a
  free port (e.g. 8003). Zero new heavy deps — vanilla JS + Python stdlib (matches existing style).
- The Console reuses the existing web channel end-to-end (inbound `POST /webui/messages` →
  `sensor.route()`; outbound SSE from the outbox) — so commands are **real**, not mocked.

## MVP (what "done tonight" means)
1. Vendor `aaka-site/brand/*` → `executor/webui/brand/` (single source of truth for the console).
2. Unified shell + nav (Console · Tasks · Status), brand-tokened.
3. Console reskinned to match the aaka.life widget (suggested-command chips, tap-to-send, streamed replies).
4. Status view: capability board from `setup_check --json` + last N log lines.
5. Taskboard reskinned (or embedded) under the same shell.
6. `admin/` launcher + `com.aaka.console.plist`.

## Out of scope (v1)
Multi-user, remote access, auth, WhatsApp pairing UI, write-actions in Status.

## Ties to other threads
- The **Console** is the real version of the aaka.life demo widget.
- The **Status capability board** is exactly the onboarding-redesign "what's on / what's next" board —
  so Claude *and* the user see the same live status. One build serves both.
