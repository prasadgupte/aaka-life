"""
Aaka — Feature Discovery Nudge

Sends one tip per onboarding level, exactly once.
"""

import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))
from skills.engagement.templates import FEATURE_TIPS

# Track which tips have been sent so we don't repeat
_SENT_KEY = "feature_tip_level"


def build_feature_tip(member_id: str, current_level: int) -> str | None:
    """Return a feature tip for the member's current level, or None if already sent."""
    from skills.engagement.engagement_db import get_state
    state = get_state(member_id)
    intents_used = set(state.get("intents_used", []))

    # Already sent a tip for this level?
    sent_key = f"tip_sent_level_{current_level}"
    # We track this as a pseudo-nudge-type in last_nudge_type
    if state.get("last_nudge_type") == sent_key:
        return None

    tip = FEATURE_TIPS.get(current_level)
    return tip  # None if no tip defined for this level
