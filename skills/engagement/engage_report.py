"""
Aaka — /engage report

Builds a per-sender engagement summary:
  • Current level + label
  • Features tried (with use counts)
  • What to try next to reach the next level
"""

import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))

# ── Level definitions (mirrored from tracker._LEVEL_GATES) ────────────────────

LEVELS = [
    {"level": 0, "label": "Newcomer",       "emoji": "🌱"},
    {"level": 1, "label": "Morning habit",  "emoji": "☀️"},
    {"level": 2, "label": "List keeper",    "emoji": "📋"},
    {"level": 3, "label": "Calendar owner", "emoji": "📅"},
    {"level": 4, "label": "Organised",      "emoji": "🗂"},
    {"level": 5, "label": "Power user",     "emoji": "⚡"},
]

# What the user needs to try to reach each level (shown when that level is next)
_NEXT_LEVEL_HINTS = {
    1: (
        "Check today's or this week's schedule *3 times*.\n"
        "Try: `t` or `w`"
    ),
    2: (
        "Use a shopping / to-do list.\n"
        "Try: `b grocery milk eggs`"
    ),
    3: (
        "Add an event to the calendar.\n"
        "Try: `c dentist Tue 3pm`"
    ),
    4: (
        "Try tasks or planning.\n"
        "Try: `/addtask call plumber` · or · `/plan friday afternoon`"
    ),
    5: (
        "Use notes or file drops.\n"
        "Try: `n idea <your thought>` · or · `d` to drop a file"
    ),
}

# Human-readable labels for every trackable intent
_FEATURE_LABELS = {
    "today_schedule":  ("Today's schedule", "t"),
    "weekly_schedule": ("Week view",         "w"),
    "buy_list":        ("Shopping lists",    "b"),
    "add_event":       ("Add event",         "c"),
    "fix_event":       ("Fix events",        "c fix / #fix"),
    "add_task":        ("Add task",          "/addtask"),
    "list_tasks":      ("Task list",         "/tasks"),
    "complete_task":   ("Complete task",     "/done"),
    "snooze_task":     ("Snooze task",       "/snooze"),
    "plan_slots":      ("Find free slots",   "/plan"),
    "block_cal":       ("Block time",        "/block"),
    "drop_note":       ("Write notes",       "n topic text"),
    "read_note":       ("Read notes",        "n topic"),
    "birthday_list":   ("Birthday lookup",   "bday"),
    "bday_wish":       ("Birthday wish",     "bday N"),
    "drop_file":       ("Smart file drop",   "f ari health #tag"),
}

# Intents that don't belong in the "features tried" section
_SKIP_DISPLAY = {"health_check", "menu", "test_status", "queue_test",
                 "executor_echo", "flush_outbox", "engage_report",
                 "llm_call", "route_tags", "test_thread"}


def _level_info(level: int) -> dict:
    return next((x for x in LEVELS if x["level"] == level), LEVELS[0])


# Gate descriptors for the ladder view — mirrors tracker._LEVEL_GATES exactly.
# Keeping this local avoids importing tracker (which logs on import).
_LADDER_GATES = [
    {
        "level":      1,
        "any_of":     ["today_schedule", "weekly_schedule"],
        "total":      3,
        "done_short": "Checked the schedule",   # past tense, shown once unlocked
        "need_label": "Check today or this week's schedule *3 times*",
        "example":    "`t`  or  `w`",
    },
    {
        "level":      2,
        "any_of":     ["buy_list"],
        "total":      1,
        "done_short": "Used shopping lists",
        "need_label": "Add anything to a shopping list",
        "example":    "`b grocery milk eggs`",
    },
    {
        "level":      3,
        "any_of":     ["add_event", "fix_event"],
        "total":      1,
        "done_short": "Added or fixed a calendar event",
        "need_label": "Add or fix one calendar event",
        "example":    "`c dentist Tue 3pm`",
    },
    {
        "level":      4,
        "any_of":     ["add_task", "list_tasks", "plan_slots", "block_cal"],
        "total":      1,
        "done_short": "Tried tasks or planning",
        "need_label": "Try tasks or find a free slot",
        "example":    "`/addtask call plumber`  or  `/plan friday`",
    },
    {
        "level":      5,
        "any_of":     ["drop_note", "drop_file"],
        "total":      1,
        "done_short": "Used notes or file drops",
        "need_label": "Write a note or drop a file",
        "example":    "`n idea <your thought>`  or  `d` with a photo",
    },
]

# Closing line when max level is reached
_MAX_LEVEL_CLOSERS = {
    0: "Just getting started — send `t` to check today.",
    1: "Good start. Try a shopping list next: `b grocery milk`",
    2: "Nice — add an event to reach the next step: `c dentist Tue 3pm`",
    3: "Almost there — try `/addtask` or `/plan` once.",
    4: "One more: write a note or drop a file.",
    5: "You've found everything. Keep using it.",
}


def _count_str(n: int) -> str:
    if n == 1:
        return "once"
    if n == 2:
        return "twice"
    return f"{n} times"


def build_ladder_report(member_id: str) -> str:
    """Return the /engage ladder — three zones: done / here / ahead."""
    import aaka_config
    from skills.engagement.engagement_db import get_state, count_uses_any, streak_days, intent_use_counts
    from skills.engagement.tracker import _maybe_advance_level

    _maybe_advance_level(member_id)

    state = get_state(member_id)
    current_level = state.get("onboarding_level", 0)

    m_obj = aaka_config.member_by_name(member_id) or {}
    name = m_obj.get("name", member_id.capitalize()).split()[0]

    streak = streak_days(member_id)
    total_cmds = sum(intent_use_counts(member_id).values())

    # ── Header: WHERE YOU ARE ─────────────────────────────────────────────────
    li = _level_info(current_level)
    lines = [f"{li['emoji']} *{name} — {li['label']}*"]

    meta_parts = []
    if streak >= 2:
        meta_parts.append(f"🔥 {streak} days running")
    elif streak == 1:
        meta_parts.append("active today")
    if total_cmds:
        meta_parts.append(f"{total_cmds} commands total")
    if meta_parts:
        lines.append("_" + "  ·  ".join(meta_parts) + "_")

    # ── Zone 1: DONE (levels below current, compressed) ─────────────────────
    done_gates = [g for g in _LADDER_GATES if g["level"] <= current_level]
    if done_gates:
        lines.append("")
        lines.append("*What you've unlocked:*")
        for gate in done_gates:
            n = count_uses_any(member_id, gate["any_of"])
            lines.append(f"  ✅  {gate['done_short']} — {_count_str(n)}")

    # ── Zone 2: NEXT (the one gate they should tackle next) ──────────────────
    next_gates = [g for g in _LADDER_GATES if g["level"] == current_level + 1]
    if next_gates:
        ng = next_gates[0]
        nli = _level_info(ng["level"])
        n = count_uses_any(member_id, ng["any_of"])
        need = ng["total"]
        lines.append("")
        lines.append(f"*To reach {nli['label']} {nli['emoji']}:*")
        if need > 1:
            lines.append(f"  {ng['need_label']}  ({n}/{need} so far)")
        else:
            lines.append(f"  {ng['need_label']}")
        lines.append(f"  → {ng['example']}")

    # ── Zone 3: AHEAD (levels beyond next, just names — don't overwhelm) ─────
    ahead_gates = [g for g in _LADDER_GATES if g["level"] > current_level + 1]
    if ahead_gates:
        ahead_labels = "  ·  ".join(
            f"{_level_info(g['level'])['emoji']} {_level_info(g['level'])['label']}"
            for g in ahead_gates
        )
        lines.append("")
        lines.append(f"_Further ahead:  {ahead_labels}_")

    # ── Closing nudge ────────────────────────────────────────────────────────
    lines.append("")
    lines.append(_MAX_LEVEL_CLOSERS.get(current_level, ""))

    return "\n".join(lines)


def build_engage_report(member_id: str) -> str:
    """Return the full /engage report string for a member."""
    import aaka_config
    from skills.engagement.engagement_db import get_state, intent_use_counts
    from skills.engagement.tracker import _maybe_advance_level

    # Recompute level from fresh event data before building report
    _maybe_advance_level(member_id)

    state = get_state(member_id)
    level = state.get("onboarding_level", 0)
    li = _level_info(level)

    m_obj = aaka_config.member_by_name(member_id) or {}
    name = m_obj.get("name", member_id.capitalize()).split()[0]

    counts = intent_use_counts(member_id)
    total_cmds = sum(counts.values())

    # ── Header ────────────────────────────────────────────────────────────────
    lines = [
        f"{li['emoji']} *{name} — Level {level}: {li['label']}*",
        f"_{total_cmds} commands total_",
        "",
    ]

    # ── Features tried ────────────────────────────────────────────────────────
    tried = {k: v for k, v in counts.items()
             if k in _FEATURE_LABELS and k not in _SKIP_DISPLAY}

    if tried:
        lines.append("*Used so far:*")
        for intent, n in sorted(tried.items(), key=lambda kv: -kv[1]):
            label, shortcut = _FEATURE_LABELS[intent]
            lines.append(f"  ✅ {label} (`{shortcut}`) — {n}×")
    else:
        lines.append("Nothing tracked yet — send `t` to start!")

    # ── Untried features worth surfacing ──────────────────────────────────────
    untried = [
        (intent, label, shortcut)
        for intent, (label, shortcut) in _FEATURE_LABELS.items()
        if intent not in tried and intent not in _SKIP_DISPLAY
    ]
    if untried:
        lines.append("")
        lines.append("*Not tried yet:*")
        for intent, label, shortcut in untried[:5]:  # cap to 5 so it stays readable
            lines.append(f"  ⬜ {label} (`{shortcut}`)")

    # ── Next level hint ───────────────────────────────────────────────────────
    next_level = level + 1
    if next_level <= max(x["level"] for x in LEVELS):
        nli = _level_info(next_level)
        hint = _NEXT_LEVEL_HINTS.get(next_level, "")
        lines.append("")
        lines.append(f"*Next → Level {next_level}: {nli['emoji']} {nli['label']}*")
        if hint:
            lines.append(hint)
    else:
        lines.append("")
        lines.append("⚡ Maximum level reached — you're a power user!")

    return "\n".join(lines)
