# Post-Setup Engagement Engine

> **Goal:** Keep users active after setup by building habits, progressively revealing
> features, and re-engaging before they churn. Privacy-first, local-only tracking.
> Deterministic logic first; LLM personalisation gated behind a flag for later.

---

## Why This Matters

The drop-off window is days 1–14. Users install, send `/today` twice, then forget.
The fix is *proactive value delivery*: push the schedule before they ask, surface the
right feature at the right moment, escalate through fallback channels if they go quiet.

---

## Feature Exposure Ladder

| Level | When | Features unlocked | Nudge trigger |
|-------|------|-------------------|---------------|
| 0 | Day 0 (setup) | `/today` `/week` | Welcome + first schedule |
| 1 | Days 1–3 | Morning brief habit | Auto-send schedule 7 am |
| 2 | Days 3–7 | `/buy` lists | "Quick tip" after 3 schedule reads |
| 3 | Week 2 | `/add` events | After first buy list use |
| 4 | Week 3 | `#fix` conflict check | After first add-event |
| 5 | Month 2+ | `/plan` `/flight` `/note` | After consistent week-3 usage |

Level is stored per-member in `engagement.db` and only advances when
the prerequisite intent has been used at least once.

---

## Nudge Types

| ID | Trigger | Channel | Rate limit |
|----|---------|---------|-----------|
| `morning_brief` | 07:00–08:00, not yet nudged today | Telegram/WhatsApp | 1×/day |
| `feature_tip` | Level threshold reached, tip not yet sent | Telegram/WhatsApp | 1×/level |
| `silence_nudge` | 3 days no activity | Telegram/WhatsApp | 1×/3 days |
| `weekly_digest` | Sunday 18:00 | Telegram/WhatsApp | 1×/week |
| `conflict_alert` | Conflict detected ≥1 day before event | Telegram/WhatsApp | 1×/conflict |
| `email_fallback` | 7 days silent + email configured | Email (Gmail API) | 1×/7 days |
| `admin_alert` | 14 days silent | Owner DM (Telegram) | 1×/7 days |

**Rate-limit guard:** max 1 nudge/member/day across all types except `conflict_alert`
(urgent).

---

## Privacy Principles

1. All data stays on the Mac home machine — nothing leaves.
2. `engagement.db` stores only: member_id (opaque), intent name, timestamp. No message
   content, no payload.
3. Email fallback uses the member's own Gmail OAuth token (already in tokens/) —
   no third-party email provider.
4. Opt-out per member: `engagement: false` in `aaka.yaml`.
5. Retention: events older than 90 days auto-purged.

---

## Architecture

```
router_sensor.py ──▶ tracker.track_event(member_id, intent)
                                    │
                          engagement.db (SQLite)
                          ┌─────────────────────┐
                          │ events (intent log)  │
                          │ member_state (level, │
                          │   last_active, etc.) │
                          └─────────────────────┘
                                    │
                         (every 15 min, launchd)
                                    │
                         nudge_runner.py
                              │
                    nudge_engine.decide(member)
                              │
              ┌───────────────┼────────────────┐
         morning_brief  feature_tip    silence_nudge
         weekly_digest  conflict_alert email_fallback
              │
         message_send.send() / email_sender.send()
```

---

## Implementation Plan

### Phase 0 — Tracking Foundation
- [x] **T01** Write plan doc (`docs/gtm/11_engagement.md`)
- [x] **T02** `skills/engagement/__init__.py` + `engagement_db.py` — SQLite schema, init, CRUD
- [x] **T03** `skills/engagement/tracker.py` — `track_event()`, `get_state()`, `set_level()`
- [x] **T04** Hook `tracker.track_event()` into `router_sensor.py` (one line per resolved intent)

### Phase 1 — Morning Brief
- [x] **T05** `skills/engagement/templates.py` — voice-aligned message templates
- [x] **T06** `skills/engagement/morning_brief.py` — reads today.md, formats nudge

### Phase 2 — Feature Discovery & Re-Engagement
- [x] **T07** `skills/engagement/feature_discovery.py` — level-gated feature tips
- [x] **T08** `skills/engagement/silence_nudge.py` — 3-day silence → re-engagement

### Phase 3 — Proactive Alerts
- [x] **T09** `skills/engagement/conflict_alert.py` — pre-event conflict detection
- [x] **T10** `skills/engagement/weekly_digest.py` — Sunday recap

### Phase 4 — Fallback Channel
- [x] **T11** `skills/engagement/email_sender.py` — Gmail API fallback (uses existing token)
- [x] **T12** Email templates in `templates.py` (7-day silence, weekly highlights)

### Phase 5 — Cron + Infra
- [x] **T13** `skills/engagement/nudge_engine.py` — decision logic (deterministic)
- [x] **T14** `skills/engagement/nudge_runner.py` — main entry point (cron calls this)
- [x] **T15** `executor/com.aaka.nudgeengine.plist` — launchd, every 15 min
- [x] **T16** Register plist + caffeinate Mac (running, PID confirmed; caffeinated 12h)
- [x] **T17** Append engagement checks to `admin/diagnose.sh`
- [x] **T18** Append engagement smoke test to `admin/test.sh`
- [x] **T19** Commit + deploy — sensor rebuilt on VPS, nudge engine live on Mac

---

## Nudge Message Samples

```
morning_brief:
  "☀️ Mon 27 Apr — 4 events today. Tight one: overlap at 13:00.
   (reply t for full schedule)"

feature_tip (level 1→2):
  "💡 Quick tip: try /buy milk eggs bread — I'll keep a running list.
   No need to text the group 🛒"

silence_nudge (3 days):
  "👋 Still here! School pickup looks busy this Thursday.
   Reply w to see the week."

weekly_digest (Sunday):
  "📅 Week recap: 12 events, 0 missed pickups, 1 conflict resolved.
   Next week: 3 events need carriers. Reply w #fix to triage."

email_fallback:
  Subject: "Your family calendar — quick check-in"
  Body: summary of next 3 events + one-liner on what they're missing
```

---

## LLM Upgrade Path (future)

Once 4+ weeks of usage data exists:
- Replace `feature_tip` selection with a lightweight LLM call that picks the most
  relevant feature based on usage history.
- Personalise `silence_nudge` body (reference a specific upcoming event).
- Gate behind `engagement_llm: true` in `aaka.yaml` (default off).

---

## Progress Log

| Date | Done |
|------|------|
| 2026-04-26 night | All 19 todos complete. Full engine live. Mac caffeinated, cron every 15 min. |
