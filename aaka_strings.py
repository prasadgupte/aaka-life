# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0
"""
aaka_strings — central registry for user-visible strings.

Pattern
-------
- Every string has a stable key (e.g. "demo.banner_title") and a value
  that may include `{placeholder}` slots filled in at call time.
- Look up via `STRINGS[key].format(...)` or the `s(key, **kwargs)` helper.
- The brand mark uses `aaka_config.branded_name()` (e.g. "&Aaka") on
  marketing/welcome surfaces; the bare `aaka_config.bot_name()` is used
  for log namespaces, file paths, and intent regex prefixes — never put
  the "&" mark in those places.

Adding new strings
------------------
- New user-visible strings touched during a refactor go here, keyed by
  surface (e.g. `email.cta_carrier`, `error.unknown_intent`).
- Per-skill template constants (e.g. `skills/engagement/templates.py`)
  remain as the home for skill-local strings; this module exists for
  cross-cutting copy and any future i18n.
- Translations are deliberately out of scope for v1 — English only.
"""

STRINGS: dict[str, str] = {
    # Demo REPL banner — see admin/demo_repl.py
    "demo.banner_title": "🌤️  {bot} Demo — The Ash-Kaa Family",

    # Engagement nudge templates — see skills/engagement/templates.py
    "engagement.email.subject":
        "Your family calendar — quick check-in from {bot}",
    "engagement.email.body":
        "Hi {name},\n\n"
        "Just checking in — {bot} hasn't heard from you in {days} days.\n\n"
        "Here's what's coming up:\n"
        "{upcoming_events}\n\n"
        "{cta}\n\n"
        "— {bot} 🌤️\n"
        "(Reply to this email isn't monitored — open Telegram or WhatsApp "
        "to chat with me)\n",
    "engagement.admin_alert":
        "🔔 {member_name} hasn't used {bot} in {days} days. "
        "Might be worth a personal check-in.",
}


def s(key: str, **kwargs) -> str:
    """Look up a string and format it. Raises KeyError on unknown key."""
    return STRINGS[key].format(**kwargs)
