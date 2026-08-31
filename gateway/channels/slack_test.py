#!/usr/bin/env python3
"""
gateway/channels/slack_test.py — mock-first Slack adapter tests (no network).
Run: python3 -m gateway.channels.slack_test
"""
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_FAILURES = []


def check(desc, cond):
    print(("  ok  " if cond else " FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


class _Resp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def main():
    from gateway.channels import slack
    from gateway.types import OutboundMessage, MessageKind

    tmp = tempfile.mkdtemp()
    os.environ["AAKA_CONFIG_DIR"] = tmp
    (Path(tmp) / "tokens").mkdir(parents=True, exist_ok=True)
    (Path(tmp) / "tokens" / "slack_workspaces.json").write_text(json.dumps({"acme": "xoxb-ACME"}))
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-DEFAULT"
    slack._ws_cache = {}; slack._ws_mtime = 0.0

    cap = {}
    orig = urllib.request.urlopen

    def fake(req, timeout=None):
        cap["url"] = req.full_url
        cap["auth"] = dict(req.header_items()).get("Authorization")
        cap["body"] = json.loads(req.data.decode())
        return _Resp({"ok": True})
    urllib.request.urlopen = fake

    slack.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="C123",
                                    channel="slack", text="hello", source="t",
                                    reply_to_message_id="1699.55"))
    check("slack: send_text hits chat.postMessage", cap["url"].endswith("/chat.postMessage"))
    check("slack: Bearer default token", cap["auth"] == "Bearer xoxb-DEFAULT")
    check("slack: channel + text in body", cap["body"]["channel"] == "C123" and cap["body"]["text"] == "hello")
    check("slack: reply → thread_ts", cap["body"].get("thread_ts") == "1699.55")

    slack.send_reaction(OutboundMessage(kind=MessageKind.REACTION, recipient="C123",
                                        channel="slack", emoji=":eyes:", reaction_message_id="1699.9", source="t"))
    check("slack: reaction hits reactions.add", cap["url"].endswith("/reactions.add"))
    check("slack: reaction name strips colons", cap["body"]["name"] == "eyes")

    slack.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="C9", channel="slack",
                                    text="hi", source="t", bot_id="acme"))
    check("slack: multi-workspace token via bot_id", cap["auth"] == "Bearer xoxb-ACME")

    urllib.request.urlopen = orig

    # egress registration
    import gateway.egress as egress
    check("egress: slack channel registered", "slack" in egress._CHANNEL_DISPATCH)

    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all slack channel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
