"""sensor/intents/engagement.py — Engagement report and birthday list handlers."""
import re

import aaka_config

HANDLES = frozenset({"engage_report", "birthday_list"})


def handle(intent: str, message: str, sender: str, channel_id: str, source: str) -> str:
    if intent == "engage_report":
        sender_member = aaka_config.member_by_sender(sender)
        if not sender_member:
            return "🔒 Engagement report requires a recognised family member."
        arg = re.sub(r'^/engage\s*|^engage\s*', '', message, flags=re.I).strip().lower()
        if arg == "ladder":
            from skills.engagement.engage_report import build_ladder_report
            return build_ladder_report(sender_member["id"])
        from skills.engagement.engage_report import build_engage_report
        return build_engage_report(sender_member["id"])

    if intent == "birthday_list":
        arg = re.sub(r'^/bday\s*|^bday\s*', '', message, flags=re.I).strip()
        from skills.contacts.birthday_list import query as _bday_query
        return _bday_query(arg, sender_id=sender)

    return "❓ Unknown engagement intent."
