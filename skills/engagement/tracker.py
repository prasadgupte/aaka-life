"""
Aaka — Engagement Tracker

Called from router_sensor.py after every intent is resolved.
Silently updates engagement.db — never raises, never blocks the main flow.

Usage:
    from skills.engagement.tracker import track_event
    track_event(member_id="alex", intent="today_schedule", channel="telegram")
"""

import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))

import logging
_log = logging.getLogger("engagement.tracker")

# Level gates: to reach level N, the member must have used ANY of the listed
# intents at least `total` times in aggregate.  Levels are sequential — level N
# is only checked once level N-1 is already held.
#
# Level labels / emojis are mirrored in engage_report.py LEVELS list.
_LEVEL_GATES = [
    # level 1: habit — checked today/week schedule at least 3 times
    {"level": 1, "any_of": ["today_schedule", "weekly_schedule"], "total": 3},
    # level 2: lists — used buy list at least once
    {"level": 2, "any_of": ["buy_list"], "total": 1},
    # level 3: calendar actor — added or fixed an event
    {"level": 3, "any_of": ["add_event", "fix_event"], "total": 1},
    # level 4: organised — tasks or planning tried
    {"level": 4, "any_of": ["add_task", "list_tasks", "plan_slots", "block_cal"], "total": 1},
    # level 5: power user — notes or file drops
    {"level": 5, "any_of": ["drop_note", "drop_file"], "total": 1},
]

# Intents to skip tracking (noise, no engagement signal)
_SKIP_INTENTS = {"health_check", "menu", "test_status", "queue_test", "executor_echo", "engage_report"}


def track_event(member_id: str, intent: str, channel: str = "") -> None:
    """Record an intent use and advance onboarding level if thresholds are met.

    Silent — catches all exceptions so the caller is never interrupted.
    """
    if not member_id or intent in _SKIP_INTENTS:
        return
    try:
        from skills.engagement.engagement_db import log_event, get_state, set_level
        log_event(member_id, intent, channel)
        _maybe_advance_level(member_id)
    except Exception as exc:
        _log.debug("track_event failed silently: %s", exc)


def _maybe_advance_level(member_id: str) -> None:
    """Re-compute the member's level from scratch using real event counts."""
    from skills.engagement.engagement_db import get_state, set_level, count_uses_any
    state = get_state(member_id)
    current = state["onboarding_level"]

    # Walk gates in order; stop at the first gate the member doesn't yet meet.
    # This lets a fresh computation catch multiple level-ups at once.
    earned = 0
    for gate in _LEVEL_GATES:
        n = count_uses_any(member_id, gate["any_of"])
        if n >= gate["total"]:
            earned = gate["level"]
        else:
            break

    if earned > current:
        set_level(member_id, earned)
        _log.info("member %s advanced from level %d to %d", member_id, current, earned)
