"""
Aaka — Nudge Message Templates

Voice-aligned: warm, direct, no corporate fluff.
Templates are plain strings with {placeholder} substitution.

Each entry:
  text   — the message body
  emoji  — leading emoji for the opening
"""

# ── Morning brief ─────────────────────────────────────────────────────────────

MORNING_BRIEF = """\
☀️ {name} — {date}
{schedule_line}
{hint}"""

MORNING_BRIEF_HINT_CONFLICT = "(heads up: clash at {time} — reply w #fix)"
MORNING_BRIEF_HINT_CARRIER  = "(missing carrier for {attendee} — reply w #fix)"
MORNING_BRIEF_HINT_QUIET    = "(reply t for full schedule)"

MORNING_NO_EVENTS = """\
☀️ {name} — {date}
Nothing on the calendar today. Enjoy the quiet."""

# ── Feature tips (one per level) ─────────────────────────────────────────────

FEATURE_TIPS = {
    2: """\
💡 Tip: try /buy milk eggs bread — I'll keep a running list. No need to text the group 🛒""",

    3: """\
💡 You can add events directly: /add Ari dentist Tue 3pm
I'll extract the time and check for conflicts before adding.""",

    4: """\
💡 Try w #fix to see the week's conflicts, missing carriers, and unaccepted invites — all in one view.""",

    5: """\
💡 Advanced: /plan Friday afternoon — I'll find free slots across everyone's calendars.
Also: /note <anything> drops a note to your vault.""",
}

# ── Silence nudge ─────────────────────────────────────────────────────────────

SILENCE_NUDGE = """\
👋 {name}, still here!
{event_teaser}
Reply w to see the week."""

SILENCE_NUDGE_NO_EVENTS = """\
👋 {name}, still here! Nothing major this week but I'm ready when you need me.
Reply /menu to see what I can do."""

# ── Weekly digest (Sunday) ────────────────────────────────────────────────────

WEEKLY_DIGEST = """\
📅 Week recap — {name}
{summary}
Next week: {next_week_line}"""

# ── Conflict alert (proactive, 1 day before) ──────────────────────────────────

CONFLICT_ALERT = """\
⚠️ Heads up, {name} — conflict tomorrow:
{detail}
Reply w #fix to sort it."""

# ── Email fallback ────────────────────────────────────────────────────────────

EMAIL_SUBJECT = "Your family calendar — quick check-in from {bot}"

EMAIL_BODY = """\
Hi {name},

Just checking in — {bot} hasn't heard from you in {days} days.

Here's what's coming up:
{upcoming_events}

{cta}

— {bot} 🌤️
(Reply to this email isn't monitored — open Telegram or WhatsApp to chat with me)
"""

EMAIL_CTA_CARRIER = "⚠️  {attendee} still needs a carrier for {event}."
EMAIL_CTA_GENERIC = "Reply /week on Telegram to see the full week."

# ── Admin alert ───────────────────────────────────────────────────────────────

ADMIN_ALERT = """\
🔔 {member_name} hasn't used {bot} in {days} days. Might be worth a personal check-in."""
