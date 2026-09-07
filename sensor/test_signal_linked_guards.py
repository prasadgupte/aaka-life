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


def test_invite_code_still_onboards_on_a_linked_device():
    """The operator's whole point in inviting someone is that they can then use
    aaka. Reading a message to look for a code is safe; replying to people who
    have no code is what was not."""
    import json as _json
    import tempfile as _tf
    from pathlib import Path as _P
    from sensor import wa_onboard

    _linked(True)
    cfg = _P(_tf.mkdtemp(prefix="aaka-invite-test-"))
    (cfg / "data").mkdir(parents=True, exist_ok=True)
    (cfg / "config").mkdir(parents=True, exist_ok=True)
    (cfg / "config" / "aaka.yaml").write_text(
        (_P(os.environ["AAKA_CONFIG_DIR"]) / "config" / "aaka.yaml").read_text())
    prev_cfg = os.environ["AAKA_CONFIG_DIR"]
    os.environ["AAKA_CONFIG_DIR"] = str(cfg)
    os.environ["SIGNAL_ACCOUNT"] = "+15550000000"
    os.environ.pop("SIGNAL_ALLOWED_CHATS", None)
    try:
        aaka_config._load.cache_clear()
        member = aaka_config.members()[0]["id"]
        code = wa_onboard.create_invite(member, "Test Person")
        stranger = "uuid-invited-person"

        check("an invited stranger is not allowed before redeeming",
              rs._chat_allowed("signal", stranger, stranger) is False)

        welcome = wa_onboard.try_signup(stranger, "Test Person", f"Hi aaka ({code})")
        check("a valid code still binds them on a linked device", bool(welcome))

        rs.grant_chat("signal", stranger)
        check("redeeming the invite opens the chat",
              rs._chat_allowed("signal", stranger, stranger) is True)

        store = _json.loads((cfg / "data" / "signal_allowed_chats.json").read_text())
        check("the grant is persisted", stranger in store)

        check("someone else with no code stays blocked",
              rs._chat_allowed("signal", "uuid-random", "uuid-random") is False)
    finally:
        os.environ["AAKA_CONFIG_DIR"] = prev_cfg
        aaka_config._load.cache_clear()


def test_whatsapp_is_linked_by_default():
    """Baileys can only ever link to a human's account — aaka cannot own a
    WhatsApp number — so WhatsApp is deny-by-default without configuration."""
    os.environ.pop("WHATSAPP_LINKED_MODE", None)
    check("whatsapp is linked by default", rs._linked_mode("whatsapp") is True)
    check("telegram is never linked", rs._linked_mode("telegram") is False)
    os.environ["WHATSAPP_LINKED_MODE"] = "false"
    check("…and can be opted out for a dedicated WhatsApp account",
          rs._linked_mode("whatsapp") is False)
    os.environ.pop("WHATSAPP_LINKED_MODE", None)

    os.environ["WHATSAPP_PHONE"] = "+15551112222"
    os.environ.pop("WHATSAPP_ALLOWED_CHATS", None)
    os.environ.pop("WHATSAPP_GROUP_JID", None)
    check("whatsapp self-chat is allowed",
          rs._chat_allowed("whatsapp", "+15551112222", "+15551112222") is True)
    check("whatsapp self-chat matches the jid spelling too",
          rs._chat_allowed("whatsapp", "15551112222@s.whatsapp.net",
                           "15551112222@s.whatsapp.net") is True)
    check("an unlisted whatsapp group is silent",
          rs._chat_allowed("whatsapp", "someone", "1203630000@g.us") is False)
    os.environ["WHATSAPP_ALLOWED_CHATS"] = "1203630000@g.us"
    check("…and speaks once listed",
          rs._chat_allowed("whatsapp", "someone", "1203630000@g.us") is True)
    os.environ.pop("WHATSAPP_ALLOWED_CHATS", None)


def main():
    test_note_to_self_is_always_allowed()
    test_invite_code_still_onboards_on_a_linked_device()
    test_whatsapp_is_linked_by_default()
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
