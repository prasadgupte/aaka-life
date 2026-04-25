"""
Aaka — Nudge Engine (Deterministic)

Decision logic: given a member and the current time, decide what nudge to send
(if any) and return a NudgeDecision. The runner executes the decision.

Priority order:
  1. conflict_alert  (urgent — not rate-limited by daily cap)
  2. morning_brief   (07:00–08:00, once/day)
  3. feature_tip     (level just advanced, not yet tipped)
  4. weekly_digest   (Sunday 18:00–19:00, once/week)
  5. silence_nudge   (3+ days silent, once per 3-day window)
  6. email_fallback  (7+ days silent, email configured)
  7. admin_alert     (14+ days silent, tell owner)

Opt-out: set `engagement: false` on a member in aaka.yaml.
Sleep hours: no nudges between 22:00 and 07:00 local time.
"""

import datetime
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
import aaka_config


@dataclass
class NudgeDecision:
    nudge_type: str
    member_id: str
    payload: dict = field(default_factory=dict)


def _local_hour(member_id: str) -> int:
    """Return current hour in the configured timezone."""
    try:
        tz_name = aaka_config.timezone()
        import zoneinfo
        now = datetime.datetime.now(zoneinfo.ZoneInfo(tz_name))
        return now.hour
    except Exception:
        return datetime.datetime.now().hour


def _local_weekday(member_id: str) -> int:
    """Return current weekday (0=Mon … 6=Sun) in configured timezone."""
    try:
        tz_name = aaka_config.timezone()
        import zoneinfo
        now = datetime.datetime.now(zoneinfo.ZoneInfo(tz_name))
        return now.weekday()
    except Exception:
        return datetime.datetime.now().weekday()


def _in_sleep_hours(member_id: str) -> bool:
    h = _local_hour(member_id)
    return h < 7 or h >= 22


def _opt_out(member_id: str) -> bool:
    """True if member has opted out of engagement nudges."""
    m_obj = aaka_config.member_by_name(member_id) or {}
    return m_obj.get("engagement") is False


def decide(member_id: str) -> Optional[NudgeDecision]:
    """Return the highest-priority nudge due for this member, or None."""
    from skills.engagement.engagement_db import (
        get_state, days_since_active, nudged_today, hours_since_nudge,
    )

    if _opt_out(member_id):
        return None

    state = get_state(member_id)
    level = state.get("onboarding_level", 0)
    last_nudge_type = state.get("last_nudge_type", "")
    silent_days = days_since_active(member_id)

    # ── 1. Conflict alert (urgent — skip sleep/daily cap) ──────────────────
    from skills.engagement.conflict_alert import build_conflict_alerts
    alerts = build_conflict_alerts(member_id)
    if alerts:
        return NudgeDecision("conflict_alert", member_id, {"messages": alerts})

    # Below here: respect sleep hours
    if _in_sleep_hours(member_id):
        return None

    # ── 2. Morning brief — SKIPPED: /t is now scheduled at 07:00 via
    #    scheduled_summaries.py, so the nudge engine no longer sends a
    #    separate compact brief.  Kept as comment for history.
    # if _local_hour(member_id) == 7 and not nudged_today(member_id):
    #     from skills.engagement.morning_brief import build_morning_brief
    #     text = build_morning_brief(member_id)
    #     if text:
    #         return NudgeDecision("morning_brief", member_id, {"text": text})

    # ── 3. Feature tip (level just changed, tip not yet sent for this level) ─
    tip_key = f"tip_sent_level_{level}"
    if level >= 2 and last_nudge_type != tip_key:
        from skills.engagement.feature_discovery import build_feature_tip
        text = build_feature_tip(member_id, level)
        if text:
            return NudgeDecision(tip_key, member_id, {"text": text})

    # ── 4. Weekly digest (Sunday 18:00–19:00, not nudged today) ────────────
    if _local_weekday(member_id) == 6 and _local_hour(member_id) == 18 and not nudged_today(member_id):
        from skills.engagement.weekly_digest import build_weekly_digest
        text = build_weekly_digest(member_id)
        if text:
            return NudgeDecision("weekly_digest", member_id, {"text": text})

    # ── 4.5. Overdue task nudge (daily at 11:00, not yet nudged today) ──────
    if _local_hour(member_id) == 11 and not nudged_today(member_id):
        try:
            from skills.tasks.local_tasks import list_open
            overdue = list_open(owner=member_id, due_filter="overdue")
            if not overdue:
                overdue = list_open(due_filter="overdue")
            if overdue:
                n = len(overdue)
                top3 = overdue[:3]
                titles = "\n".join(f"  • {t['title']}" for t in top3)
                more = f"\n  +{n - 3} more" if n > 3 else ""
                text = (
                    f"⚠️ You have {n} overdue task{'s' if n != 1 else ''}:\n"
                    f"{titles}{more}\n"
                    f"Reply `/tasks overdue` to see all."
                )
                return NudgeDecision("overdue_task_nudge", member_id, {"text": text})
        except Exception:
            pass

    # ── 5. Silence nudge (3+ days, not nudged in last 3 days) ──────────────
    if silent_days >= 3 and hours_since_nudge(member_id) >= 72:
        from skills.engagement.silence_nudge import build_silence_nudge
        text = build_silence_nudge(member_id)
        if text:
            return NudgeDecision("silence_nudge", member_id, {"text": text})

    # ── 6. Email fallback (7+ days, email not yet sent in last 7 days) ──────
    if silent_days >= 7:
        from skills.engagement.engagement_db import get_state as _gs
        _st = _gs(member_id)
        last_email = _st.get("email_fallback_ts", "")
        should_email = True
        if last_email:
            try:
                last_dt = datetime.datetime.fromisoformat(last_email)
                should_email = (datetime.datetime.utcnow() - last_dt).days >= 7
            except Exception:
                pass
        if should_email:
            m_obj = aaka_config.member_by_name(member_id) or {}
            if m_obj.get("email"):
                return NudgeDecision("email_fallback", member_id, {})

    # ── 7. Admin alert (14+ days, owner gets a DM) ──────────────────────────
    if silent_days >= 14:
        _st = get_state(member_id)
        last_admin = _st.get("admin_alert_ts", "")
        should_alert = True
        if last_admin:
            try:
                last_dt = datetime.datetime.fromisoformat(last_admin)
                should_alert = (datetime.datetime.utcnow() - last_dt).days >= 7
            except Exception:
                pass
        if should_alert:
            return NudgeDecision("admin_alert", member_id, {"days": int(silent_days)})

    return None
