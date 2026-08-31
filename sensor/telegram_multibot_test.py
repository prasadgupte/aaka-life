#!/usr/bin/env python3
"""
sensor/telegram_multibot_test.py — mock-first tests (no processes, no network).

Verifies resolve_bots() config resolution + that the poller keys its offset file
and inbound metadata by AAKA_BOT_ID. Run: python3 -m sensor.telegram_multibot_test
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_FAILURES = []


def check(desc, cond):
    print(("  ok  " if cond else " FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


def main():
    tmp = tempfile.mkdtemp()
    os.environ["AAKA_CONFIG_DIR"] = tmp
    (Path(tmp) / "tokens").mkdir(parents=True, exist_ok=True)

    import importlib
    import sensor.telegram_multibot as mb
    importlib.reload(mb)

    # 1) No config, default token → single default bot
    os.environ["TELEGRAM_BOT_TOKEN"] = "DEFTOK"
    check("resolve: default-only → [('', DEFTOK)]", mb.resolve_bots() == [("", "DEFTOK")])

    # 2) No config, no token → empty
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    check("resolve: no token → []", mb.resolve_bots() == [])

    # 3) Multi-bot config → one entry per bot
    (Path(tmp) / "tokens" / "telegram_bots.json").write_text(
        json.dumps({"family": "FAMTOK", "demo": "DEMOTOK"}))
    got = dict(mb.resolve_bots())
    check("resolve: multi-bot from json", got == {"family": "FAMTOK", "demo": "DEMOTOK"})

    # 4) Poller keys offset file + metadata by AAKA_BOT_ID
    os.environ["AAKA_BOT_ID"] = "demo"
    os.environ["TELEGRAM_BOT_TOKEN"] = "x"
    import sensor.telegram_poller as poller
    importlib.reload(poller)
    check("poller: per-bot offset file name", poller._OUR_OFFSET_FILE.name == "telegram_offset_demo.json")
    meta_blob = poller._build_format_a("42", "99", 1, "hi")
    check("poller: bot_id in Format A metadata", '"bot_id": "demo"' in meta_blob)

    # default bot → plain offset file, no bot_id in metadata
    os.environ.pop("AAKA_BOT_ID", None)
    importlib.reload(poller)
    check("poller: default offset file name", poller._OUR_OFFSET_FILE.name == "telegram_offset.json")
    check("poller: no bot_id for default", '"bot_id"' not in poller._build_format_a("42", "99", 1, "hi"))

    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all multibot supervisor checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
