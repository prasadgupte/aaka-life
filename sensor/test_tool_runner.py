#!/usr/bin/env python3
"""sensor/test_tool_runner.py — aaka Tools runner (no network for the core checks)."""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_TMP = Path(tempfile.mkdtemp(prefix="aaka-tools-"))
(_TMP / "config").mkdir(parents=True, exist_ok=True)
(_TMP / "logs").mkdir(parents=True, exist_ok=True)
(_TMP / "config" / "aaka.yaml").write_text(
    "system: {bot_name: 'Rosi'}\nmembers:\n  - {id: 'alex', name: 'Alex', admin: true}\n"
)
os.environ["AAKA_CONFIG_DIR"] = str(_TMP)

# a mock tool that emits a structured result
_mock = _TMP / "mock_tool.py"
_mock.write_text(
    "import json,os\n"
    "print(json.dumps({'ok': os.environ.get('AAKA_TOOL_SECRETS','')!='', "
    "'summary': 'secrets='+os.environ.get('AAKA_TOOL_SECRETS','none'), 'error': None}))\n"
)
(_TMP / "config" / "tools.yaml").write_text(
    f"mock:\n  run: {_mock}\n  secrets: mocksecret\n  report_to: alex\n  enabled: true\n"
    f"disabled_one:\n  run: {_mock}\n  enabled: false\n"
)

import sensor.tool_runner as tr  # noqa: E402

_FAIL = []
def check(d, c):
    print(f"  {'ok  ' if c else 'FAIL'} {d}")
    if not c:
        _FAIL.append(d)


def main():
    man = tr.load_manifest()
    check("manifest loads tools.yaml", "mock" in man and "disabled_one" in man)

    res = tr.run_tool("mock", man["mock"])
    check("run_tool parses structured result", res.get("ok") is True)
    check("secrets dir injected as AAKA_TOOL_SECRETS", "mocksecret" in res.get("summary", ""))

    logp = _TMP / "logs" / "tools" / "mock.jsonl"
    check("run logged to logs/tools/<name>.jsonl", logp.exists() and "ok" in logp.read_text())

    disabled = tr.run_and_report("disabled_one")
    check("disabled tool not run", disabled.get("error") == "disabled")

    unknown = tr.run_tool("nope", {})
    check("unknown tool → no_such_tool", unknown.get("error") == "no_such_tool")

    # cron matcher
    d = datetime(2026, 9, 2, 7, 0)          # Wed 07:00
    check("cron '0 7 * * *' matches 07:00", tr._cron_matches("0 7 * * *", d))
    check("cron '*/30 * * * *' matches :00", tr._cron_matches("*/30 * * * *", d))
    check("cron '0 8 * * *' does not match 07:00", not tr._cron_matches("0 8 * * *", d))
    check("cron dow: Wed is 3", tr._cron_matches("0 7 * * 3", d))

    # report path is best-effort and must never raise (no telegram token here)
    try:
        tr.report("mock", man["mock"], {"ok": False, "error": "auth_required", "summary": "login failed"})
        check("report() never raises (auth_required path)", True)
    except Exception as e:
        check(f"report() never raises: {e}", False)

    print()
    if _FAIL:
        print(f"FAILED: {len(_FAIL)}")
        return 1
    print("all tool_runner checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
