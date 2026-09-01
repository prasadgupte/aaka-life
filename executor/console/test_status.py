#!/usr/bin/env python3
"""
executor/console/test_status.py — Status route tests (mock subprocess + temp logs).

No network, no real setup_check.py run: the subprocess is monkeypatched.
Run: /Users/Shared/aaka-repo/venv/bin/python3 executor/console/test_status.py
     (exit 0 = pass)
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

_TMP_CONFIG = Path(tempfile.mkdtemp(prefix="aaka-console-status-"))
(_TMP_CONFIG / "config").mkdir(parents=True, exist_ok=True)
(_TMP_CONFIG / "data" / "queue").mkdir(parents=True, exist_ok=True)
(_TMP_CONFIG / "config" / "aaka.yaml").write_text(
    "timezone: Europe/Berlin\nmembers:\n  - id: alex\n    name: Alex\n    telegram: '111'\n",
    encoding="utf-8",
)
os.environ["AAKA_CONFIG_DIR"] = str(_TMP_CONFIG)
os.environ["AAKA_BASE"] = str(REPO_ROOT)
os.environ["QUEUE_DB"] = str(_TMP_CONFIG / "data" / "queue" / "butler.db")

from fastapi.testclient import TestClient  # noqa: E402

from executor.console import server as console  # noqa: E402

console.State.config_dir = _TMP_CONFIG
client = TestClient(console.app)

_FAILURES = []


def check(desc, cond):
    print(("  ok  " if cond else " FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


class _FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _patch_subprocess(returncode, stdout=b"", stderr=b""):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(returncode, stdout, stderr)
    console.asyncio.create_subprocess_exec = fake_exec


def _patch_subprocess_timeout():
    async def fake_exec(*args, **kwargs):
        return _FakeProc(0, b"{}")
    console.asyncio.create_subprocess_exec = fake_exec

    orig_wait_for = asyncio.wait_for

    async def fake_wait_for(coro, timeout):
        # Consume the coroutine to avoid "never awaited" warnings, then raise.
        try:
            await coro
        except Exception:
            pass
        raise asyncio.TimeoutError()
    console.asyncio.wait_for = fake_wait_for
    return orig_wait_for


def main():
    orig_exec = console.asyncio.create_subprocess_exec
    orig_wait_for = console.asyncio.wait_for

    # ── /console/status/data — happy path ──────────────────────────────────
    canned = json.dumps({
        "tiers": {"0": {"label": "Core", "reachable": True, "failing": [], "next_fix": ""}},
        "checks": [
            {"id": "config_dir", "tier": 0, "label": "Config directory", "ok": True,
             "value": "/tmp/x", "fix": "", "note": ""},
        ],
    }).encode()
    _patch_subprocess(0, canned)
    r = client.get("/console/status/data")
    check("status/data returns 200 (happy)", r.status_code == 200)
    j = r.json()
    check("status/data has checks list", isinstance(j.get("checks"), list))
    check("status/data has tiers", "tiers" in j)

    # ── non-zero exit ──────────────────────────────────────────────────────
    _patch_subprocess(1, b"", b"boom")
    r = client.get("/console/status/data")
    check("status/data nonzero exit returns error", "error" in r.json())

    # ── timeout ────────────────────────────────────────────────────────────
    _patch_subprocess_timeout()
    r = client.get("/console/status/data")
    j = r.json()
    check("status/data timeout returns error", "error" in j)
    check("status/data timeout mentions timeout", "timeout" in j.get("error", "").lower())
    console.asyncio.create_subprocess_exec = orig_exec
    console.asyncio.wait_for = orig_wait_for

    # ── /console/status/logs — 30 lines → last 20 ──────────────────────────
    logs_dir = _TMP_CONFIG / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"line-{i}" for i in range(30)]
    (logs_dir / "queueworker.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = client.get("/console/status/logs")
    check("status/logs returns 200", r.status_code == 200)
    j = r.json()
    check("queueworker tail has 20 lines", len(j.get("queueworker", [])) == 20)
    check("queueworker tail last line correct", j["queueworker"][-1] == "line-29")
    check("sensor key present (empty, file absent)", j.get("sensor") == [])

    # ── missing log dir → empty arrays ─────────────────────────────────────
    empty_cfg = Path(tempfile.mkdtemp(prefix="aaka-console-nologs-"))
    console.State.config_dir = empty_cfg
    r = client.get("/console/status/logs")
    j = r.json()
    check("missing logs → queueworker empty", j.get("queueworker") == [])
    check("missing logs → sensor empty", j.get("sensor") == [])
    console.State.config_dir = _TMP_CONFIG

    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all console status checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
