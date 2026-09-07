#!/usr/bin/env python3
"""
sensor/test_signal_linked_guards.py — the two guards that make linked-device
Signal safe to run on a human's personal account.

Both exist because of a real incident: with aaka linked to the operator's own
Signal, a stranger messaged them, and aaka answered from their personal account
with the "you're almost in" onboarding text — twice. The operator also has their
kids' school group on that account, where an accidental one-letter shortcut
would have made aaka reply in front of every other parent.

  1. unknown senders get silence (never an onboarding reply, never a signup)
  2. groups are allowed only when explicitly configured, even when the sender
     is a known member

Run: python3 sensor/test_signal_linked_guards.py   (exit 0 = pass)
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
# Demo config: a real member roster without touching the operator's own.
os.environ["AAKA_CONFIG_DIR"] = str(REPO_ROOT / "samples" / "demo")

import aaka_config  # noqa: E402
import sensor.router_sensor as rs  # noqa: E402

_FAILURES = []


def check(desc, cond):
    if cond:
        print(f"  ok   {desc}")
    else:
        print(f"  FAIL {desc}")
        _FAILURES.append(desc)


def _member_handle():
    """Any handle the demo roster recognises, so 'known sender' is realistic."""
    for m in aaka_config.members():
        for field in ("signal", "signal_number", "whatsapp", "telegram_id", "telegram"):
            v = m.get(field)
            if v:
                return str(v)
    return ""


def _linked(on: bool):
    os.environ["SIGNAL_LINKED_MODE"] = "true" if on else "false"


def test_note_to_self_is_always_allowed():
    """The operator's own chat is their only way to reach aaka on a linked
    device, and nobody else can see it."""
    _linked(True)
    os.environ["SIGNAL_ACCOUNT"] = "+15550000000"
    os.environ.pop("SIGNAL_ALLOWED_CHATS", None)
    os.environ.pop("SIGNAL_GROUP_ID", None)
    check("note to self allowed with nothing configured",
          rs._is_allowed_channel("+15550000000", "+15550000000", "signal") is True)


def test_known_member_dm_is_not_implicit_consent():
    """A member DMing the operator is not consent: the reply would look like it
    came from the operator, in their private conversation."""
    known = _member_handle()
    _linked(True)
    os.environ["SIGNAL_ACCOUNT"] = "+15550000000"
    os.environ.pop("SIGNAL_ALLOWED_CHATS", None)
    os.environ.pop("SIGNAL_GROUP_ID", None)
    check("known member's DM is blocked until opted in",
          rs._is_allowed_channel(known, known, "signal") is False)
    os.environ["SIGNAL_ALLOWED_CHATS"] = known
    check("…and allowed once listed",
          rs._is_allowed_channel(known, known, "signal") is True)
    os.environ.pop("SIGNAL_ALLOWED_CHATS", None)


def test_allowlist_accepts_either_group_spelling():
    _linked(True)
    os.environ["SIGNAL_ACCOUNT"] = "+15550000000"
    os.environ["SIGNAL_ALLOWED_CHATS"] = "FAMILYID=="
    check("bare group id matches the group: form",
          rs._is_allowed_channel("someone", "group:FAMILYID==", "signal") is True)
    os.environ["SIGNAL_ALLOWED_CHATS"] = "group:FAMILYID=="
    check("group: form matches the bare id",
          rs._is_allowed_channel("someone", "FAMILYID==", "signal") is True)
    check("the school group is still blocked",
          rs._is_allowed_channel("someone", "group:SCHOOLID==", "signal") is False)
    os.environ.pop("SIGNAL_ALLOWED_CHATS", None)


def test_groups_need_explicit_config_in_linked_mode():
    known = _member_handle()
    check("demo roster yields a known handle", bool(known))
    prev_group = os.environ.get("SIGNAL_GROUP_ID")
    try:
        _linked(True)
        os.environ.pop("SIGNAL_GROUP_ID", None)
        # The school-group case: the OPERATOR posts, so the sender is a member.
        allowed = rs._is_allowed_channel(known, "group:SCHOOLGROUPID==", "signal")
        check("unconfigured group is blocked even for a known sender", allowed is False)

        os.environ["SIGNAL_GROUP_ID"] = "FAMILYGROUPID=="
        check("the configured family group is allowed",
              rs._is_allowed_channel(known, "group:FAMILYGROUPID==", "signal") is True)
        check("a different group is still blocked",
              rs._is_allowed_channel(known, "group:SCHOOLGROUPID==", "signal") is False)
        # A member DM is now opt-in too (see the dedicated test above) — the
        # group allowlist is not the only thing that got stricter.
        check("member DMs are opt-in, not implicit",
              rs._is_allowed_channel(known, known, "signal") is False)
    finally:
        if prev_group is None:
            os.environ.pop("SIGNAL_GROUP_ID", None)
        else:
            os.environ["SIGNAL_GROUP_ID"] = prev_group


def test_dedicated_number_keeps_the_old_group_behaviour():
    """Not linked = aaka has its own account and is only in groups it was added
    to, so the member shortcut stays."""
    known = _member_handle()
    _linked(False)
    os.environ.pop("SIGNAL_GROUP_ID", None)
    check("unlinked: known sender in a group is allowed as before",
          rs._is_allowed_channel(known, "group:ANYGROUP==", "signal") is True)


def test_unknown_sender_is_answered_with_silence_in_linked_mode():
    _linked(True)
    envelope = (
        'Conversation info (untrusted metadata):\n```json\n'
        '{"chat_id": "signal:uuid-stranger", "message_id": "1", '
        '"sender_id": "uuid-stranger", "conversation_label": "id:uuid-stranger"}\n```\n'
        'Hi'
    )
    out = rs.route(envelope)
    check("stranger gets no reply at all", out == "")
    check("no onboarding text leaks", "almost in" not in (out or ""))


def test_unknown_sender_still_onboarded_for_a_dedicated_number():
    """Regression guard: the onboarding reply is correct when aaka owns the
    account, and must not be collateral damage from the linked-mode fix."""
    _linked(False)
    envelope = (
        'Conversation info (untrusted metadata):\n```json\n'
        '{"chat_id": "signal:uuid-stranger2", "message_id": "2", '
        '"sender_id": "uuid-stranger2", "conversation_label": "id:uuid-stranger2"}\n```\n'
        'Hi'
    )
    out = rs.route(envelope)
    check("dedicated number still tells a stranger how to be added",
          "almost in" in (out or ""))


def main():
    test_note_to_self_is_always_allowed()
    test_known_member_dm_is_not_implicit_consent()
    test_allowlist_accepts_either_group_spelling()
    test_groups_need_explicit_config_in_linked_mode()
    test_dedicated_number_keeps_the_old_group_behaviour()
    test_unknown_sender_is_answered_with_silence_in_linked_mode()
    test_unknown_sender_still_onboarded_for_a_dedicated_number()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all linked-mode guard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
