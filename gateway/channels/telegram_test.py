#!/usr/bin/env python3
"""
gateway/channels/telegram_test.py — mock-first tests (no network).

Covers:
  • multi-bot token routing (env override, json map, fallback to default)
  • Phase 2: adapter.send_message routes Telegram → egress, WhatsApp → CLI, dry_run prints

Run: python3 -m gateway.channels.telegram_test   (exit 0 = pass)
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


def test_multibot_tokens():
    from gateway.channels import telegram as tg
    from gateway.types import OutboundMessage, MessageKind

    tmp = tempfile.mkdtemp()
    os.environ["AAKA_CONFIG_DIR"] = tmp
    (Path(tmp) / "tokens").mkdir(parents=True, exist_ok=True)
    (Path(tmp) / "tokens" / "telegram_bots.json").write_text(json.dumps({"family": "FAMTOK"}))
    os.environ["TELEGRAM_BOT_TOKEN"] = "DEFAULTTOK"
    os.environ["TELEGRAM_BOT_TOKEN_DEMO"] = "DEMOTOK"
    tg._bots_cache = {}; tg._bots_mtime = 0.0  # bust cache

    cap = {"urls": []}
    orig = urllib.request.urlopen

    def fake(req, timeout=None):
        cap["urls"].append(req.full_url)
        return _Resp({"ok": True, "result": {}})
    urllib.request.urlopen = fake

    def send(bot_id):
        cap["urls"].clear()
        tg.send_text(OutboundMessage(kind=MessageKind.TEXT, recipient="123",
                                     channel="telegram", text="hi", source="t", bot_id=bot_id))
        return cap["urls"][-1]

    check("multibot: bot_id=None uses default token", "/botDEFAULTTOK/" in send(None))
    check("multibot: env override TELEGRAM_BOT_TOKEN_DEMO", "/botDEMOTOK/" in send("demo"))
    check("multibot: json map (family→FAMTOK)", "/botFAMTOK/" in send("family"))
    check("multibot: unknown bot falls back to default", "/botDEFAULTTOK/" in send("nope"))

    urllib.request.urlopen = orig


def test_adapter_routing():
    import gateway.egress as egress
    import gateway.adapter as adapter_mod
    from gateway.adapter import GatewayAdapter

    # Telegram → egress (native, no CLI)
    captured = {}
    orig_send = egress.send
    egress.send = lambda m: captured.update(msg=m)
    GatewayAdapter().send_message("telegram", "555", "hello tg")
    egress.send = orig_send
    m = captured.get("msg")
    check("phase2: telegram routes through egress", m is not None and m.channel == "telegram")
    check("phase2: egress msg carries text", m is not None and m.text == "hello tg")

    # WhatsApp → egress too (native, via the wa-sidecar; no claw CLI)
    captured_wa = {}
    egress.send = lambda m: captured_wa.update(msg=m)
    GatewayAdapter().send_message("whatsapp", "+31600", "hello wa")
    egress.send = orig_send
    wm = captured_wa.get("msg")
    check("phase3: whatsapp routes through egress (no claw CLI)",
          wm is not None and wm.channel == "whatsapp" and wm.text == "hello wa")

    # dry_run prints, sends nothing
    sent = {}
    egress.send = lambda m: sent.update(hit=True)
    GatewayAdapter().send_message("telegram", "555", "x", dry_run=True)
    egress.send = orig_send
    check("phase2: dry_run does not send", "hit" not in sent)


def main():
    test_multibot_tokens()
    test_adapter_routing()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all telegram channel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
