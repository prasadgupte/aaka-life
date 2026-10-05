#!/usr/bin/env python3
"""
gateway/agent_api_llm_test.py — /v1/llm image contract, no network, no claude binary.

Covers: request validation (413 / 422), the stream-json parser that decides
`saw_images`, the "read nothing → fall through to Gemini" rule, and the
vision_unsupported 422. Run: venv/bin/python3 gateway/agent_api_llm_test.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["AAKA_CONFIG_DIR"] = tempfile.mkdtemp(prefix="aaka-llm-test-")

from fastapi.testclient import TestClient  # noqa: E402

from gateway import agent_api as api  # noqa: E402
from gateway.llm_providers import VisionUnsupported  # noqa: E402

_FAILURES = []


def check(desc, cond):
    print(("  ok  " if cond else " FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


# The image-contract cases below exercise the Gemini fallback, so the test agent
# holds a grant; the policy section at the end swaps in agents without one.
GRANTED = {"id": "test-agent", "permissions": json.dumps({"gemini": {"daily_max": 100}})}
AGENT = dict(GRANTED)
api.app.dependency_overrides[api._require_agent] = lambda: AGENT
client = TestClient(api.app)
HDR = {"X-Agent-Key": "x"}
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def post(**body):
    body.setdefault("prompt", "what is in Image 1?")
    body.setdefault("response_format", "text")
    return client.post("/v1/llm", json=body, headers=HDR)


def stream(paths_read, result="the answer", image_result=True, error=False, denied=False):
    """Fabricate claude --output-format stream-json lines."""
    lines = [{"type": "system", "subtype": "init"}]
    blocks, results = [], []
    for i, p in enumerate(paths_read):
        tid = f"toolu_{i}"
        blocks.append({"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": p}})
        if denied:
            results.append({"type": "tool_result", "tool_use_id": tid, "is_error": True,
                            "content": "Claude requested permissions to read"})
        else:
            results.append({"type": "tool_result", "tool_use_id": tid,
                            "content": [{"type": "image"}] if image_result else "text only"})
    if blocks:
        lines.append({"type": "assistant", "message": {"content": blocks}})
        lines.append({"type": "user", "message": {"content": results}})
    lines.append({"type": "assistant", "message": {"content": [{"type": "text", "text": result}]}})
    lines.append({"type": "result", "subtype": "error_during_execution" if error else "success",
                  "is_error": error, "result": result})
    return "\n".join(json.dumps(l) for l in lines)


class FakeRun:
    """Stands in for subprocess.run; records argv and answers with canned stdout."""
    def __init__(self):
        self.calls = []
        self.stdout_for = lambda argv, kw: stream([])
        self.returncode, self.stderr = 0, ""

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        out = self.stdout_for(argv, kw)
        return subprocess.CompletedProcess(argv, self.returncode, stdout=out, stderr=self.stderr)


def policy_checks(fake, gemini_calls, fake_gemini, log):
    """Gemini is a per-agent grant with a daily cap; a Claude usage limit pauses Claude."""
    from gateway import llm_policy
    state = Path(os.environ["AAKA_CONFIG_DIR"]) / "data" / "llm_policy.json"
    api._call_gemini_fallback = fake_gemini
    state.unlink(missing_ok=True)

    # ── no grant: a Claude error is a 503 that says why; Gemini is never called ──
    AGENT.clear(); AGENT.update({"id": "batch-agent", "permissions": "{}"})
    fake.returncode, fake.stderr = 1, ""
    fake.stdout_for = lambda argv, kw: json.dumps({"type": "result", "is_error": True,
                                                   "result": "API Error: 500 internal"})
    before = len(gemini_calls)
    r = post()
    d = r.json().get("detail", {})
    check("no grant + claude error → 503 claude_unavailable",
          r.status_code == 503 and d.get("error") == "claude_unavailable" and d.get("reason") == "claude_error")
    check("…the 503 carries claude's own reason (stdout result, stderr empty)", "API Error: 500" in d.get("detail", ""))
    check("…and Gemini is not called", len(gemini_calls) == before)
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    check("the claude failure is logged with its reason",
          rows[-1]["provider"] == "claude" and rows[-1]["status"] == "error"
          and "API Error: 500" in rows[-1].get("error", "") and rows[-1]["agent_id"] == "batch-agent")
    check("a transient error does not pause Claude", llm_policy.claude_paused() is None)

    # ── exit 0 but is_error → still a failure, never returned as the answer ──
    fake.returncode = 0
    r = post()
    check("exit 0 with is_error=true → 503, not a 200 carrying the error text", r.status_code == 503)

    # ── usage limit: pause Claude, answer at once until it lifts ──
    fake.returncode = 1
    fake.stdout_for = lambda argv, kw: json.dumps({"type": "result", "is_error": True,
                                                   "result": "Claude AI usage limit reached|1760000000"})
    r = post()
    d = r.json().get("detail", {})
    check("usage limit → 503 reason claude_limit with Retry-After",
          r.status_code == 503 and d.get("reason") == "claude_limit" and int(r.headers.get("retry-after", 0)) > 0)
    check("…and Claude is paused", llm_policy.claude_paused() is not None)
    n = len(fake.calls)
    r = post()
    check("while paused no claude process is started", len(fake.calls) == n and r.status_code == 503)

    # ── grant: Gemini during the pause, with the reason logged, up to the cap ──
    AGENT.clear(); AGENT.update({"id": "bot", "permissions": json.dumps({"gemini": {"daily_max": 2}})})
    r1, r2, r3 = post(), post(), post()
    check("granted agent → Gemini while Claude is paused",
          r1.status_code == 200 and r1.json()["provider"] == "gemini" and r2.status_code == 200)
    check("…third call over daily_max 2 → 429 gemini_daily_cap",
          r3.status_code == 429 and r3.json()["detail"].get("error") == "gemini_daily_cap"
          and r3.json()["detail"].get("max") == 2)
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    ok_gem = [x for x in rows if x.get("provider") == "gemini" and x["status"] == "ok" and x["agent_id"] == "bot"]
    check("Gemini rows record the fallback_reason", ok_gem and "usage limit" in ok_gem[-1].get("fallback_reason", ""))
    check("the day's count is kept per agent", llm_policy.gemini_used_today().get("bot") == 2)

    # ── pause over → Claude again ──
    state.unlink(missing_ok=True)
    fake.returncode, fake.stdout_for = 0, (lambda argv, kw: json.dumps({"result": "back"}))
    r = post()
    check("pause lifted → Claude answers again", r.status_code == 200 and r.json()["provider"] == "claude")

    # ── grant parsing ──
    check("grant: dict permissions", llm_policy.gemini_grant({"permissions": {"gemini": {"daily_max": 5}}}) == 5)
    check("grant: none / zero / junk → no grant",
          llm_policy.gemini_grant({"permissions": "{}"}) is None
          and llm_policy.gemini_grant({"permissions": json.dumps({"gemini": {"daily_max": 0}})}) is None
          and llm_policy.gemini_grant({"permissions": "not json"}) is None
          and llm_policy.gemini_grant({}) is None)

    # ── direct calls: only inside an allowed scope ──
    os.environ.pop("AAKA_GEMINI_INTENTS", None)
    try:
        llm_policy.check_direct_gemini()
        check("direct Gemini with no scope is refused", False)
    except llm_policy.GeminiNotAllowed:
        check("direct Gemini with no scope is refused", True)
    with llm_policy.scope("add_event"):
        llm_policy.check_direct_gemini()
        check("add_event (live /cal) may call Gemini directly by default", True)
    with llm_policy.scope("add_task"):
        try:
            llm_policy.check_direct_gemini()
            check("add_task is refused by default", False)
        except llm_policy.GeminiNotAllowed:
            check("add_task is refused by default", True)
    os.environ["AAKA_GEMINI_INTENTS"] = "add_event, add_task"
    with llm_policy.scope("add_task"):
        llm_policy.check_direct_gemini()
        check("AAKA_GEMINI_INTENTS adds services", True)
    os.environ.pop("AAKA_GEMINI_INTENTS", None)
    check("scope resets after the block", llm_policy.current_scope() is None)

    AGENT.clear(); AGENT.update(GRANTED)
    fake.returncode, fake.stderr = 0, ""


def main():
    orig_run, orig_which, orig_gemini = api.subprocess.run, api.shutil.which, api._call_gemini_fallback
    api.shutil.which = lambda name: "/usr/bin/claude"
    fake = FakeRun()
    api.subprocess.run = fake
    gemini_calls = []

    def fake_gemini(prompt, timeout, images=None):
        gemini_calls.append(images)
        return "gemini says", "gemini-test"
    api._call_gemini_fallback = fake_gemini

    # ── validation ──
    r = post(images=[{"data": PNG}] * 5)
    check("5 images → 413 too_many_images", r.status_code == 413 and r.json()["detail"]["error"] == "too_many_images")
    r = post(images=[{"data": "not base64!!"}])
    check("bad base64 → 422 bad_image", r.status_code == 422 and r.json()["detail"]["error"] == "bad_image")
    big = base64.b64encode(b"\x00" * (2 * 1024 * 1024 + 1)).decode()
    r = post(images=[{"data": big}])
    check("2 MB + 1 → 413 image_too_large", r.status_code == 413 and r.json()["detail"]["error"] == "image_too_large")
    r = post(images=[{"data": PNG, "media_type": "image/gif"}])
    check("gif media_type → 422 (pydantic)", r.status_code == 422)
    check("no claude call made for rejected requests", fake.calls == [])

    # ── no images: legacy json path untouched ──
    fake.stdout_for = lambda argv, kw: json.dumps({"result": "plain answer"})
    r = post()
    argv = fake.calls[-1][0]
    check("no images → 200 with saw_images 0", r.status_code == 200 and r.json()["saw_images"] == 0)
    check("no images → --output-format json, no --tools", "json" in argv and "--tools" not in argv)
    check("no images → text passthrough", r.json()["text"] == "plain answer")

    # ── images: happy path ──
    seen_dirs = []

    def with_images(argv, kw):
        tmp = kw["cwd"]
        seen_dirs.append(tmp)
        check("image file written into private temp dir", (Path(tmp) / "1.png").exists()
              and (Path(tmp) / "2.jpg").exists())
        check("cli runs with only the Read tool", argv[argv.index("--tools") + 1] == "Read")
        check("Read allowed only inside the temp dir (// absolute rule)",
              argv[argv.index("--allowedTools") + 1] == f"Read(/{tmp}/**)")
        prompt = argv[argv.index("-p") + 1]
        check("prompt lists labelled image paths",
              f"Image 1 (the figure): {tmp}/1.png" in prompt and f"Image 2: {tmp}/2.jpg" in prompt)
        check("caller prompt kept after the listing", prompt.endswith("what is in Image 1?"))
        return stream([f"{tmp}/1.png", f"{tmp}/2.jpg"], result="vision answer")
    fake.stdout_for = with_images
    r = post(images=[{"data": PNG, "label": "the figure"},
                     {"data": PNG, "media_type": "image/jpeg"}])
    check("images → 200 from claude", r.status_code == 200 and r.json()["provider"] == "claude")
    check("images → saw_images 2", r.json()["saw_images"] == 2)
    check("images → text from stream result", r.json()["text"] == "vision answer")
    check("temp dir removed afterwards", seen_dirs and not Path(seen_dirs[-1]).exists())
    check("gemini not called on success", gemini_calls == [])

    # ── partial read is reported honestly ──
    fake.stdout_for = lambda argv, kw: stream([f"{kw['cwd']}/1.png"], result="half")
    r = post(images=[{"data": PNG}, {"data": PNG}])
    check("read 1 of 2 → saw_images 1", r.status_code == 200 and r.json()["saw_images"] == 1)

    # ── read nothing → fall through to gemini with the images ──
    fake.stdout_for = lambda argv, kw: stream([], result="blind guess")
    r = post(images=[{"data": PNG, "label": "fig"}])
    check("cli read none → gemini fallback", r.status_code == 200 and r.json()["provider"] == "gemini")
    check("fallback carries the images", gemini_calls and gemini_calls[-1][0].label == "fig")
    check("fallback reports saw_images = all", r.json()["saw_images"] == 1)

    fake.stdout_for = lambda argv, kw: stream([f"{kw['cwd']}/1.png"], denied=True, result="need permission")
    r = post(images=[{"data": PNG}])
    check("permission-denied Read does not count → gemini", r.json()["provider"] == "gemini")

    fake.stdout_for = lambda argv, kw: stream([f"{kw['cwd']}/1.png"], image_result=False, result="x")
    r = post(images=[{"data": PNG}])
    check("Read result without image block does not count → gemini", r.json()["provider"] == "gemini")

    fake.stdout_for = lambda argv, kw: stream([f"{kw['cwd']}/1.png"], error=True, result="boom")
    r = post(images=[{"data": PNG}])
    check("stream result error → gemini", r.json()["provider"] == "gemini")

    # ── provider without vision → 422 vision_unsupported ──
    def no_vision(prompt, timeout, images=None):
        raise VisionUnsupported("claude-cli")
    api._call_gemini_fallback = no_vision
    fake.stdout_for = lambda argv, kw: stream([], result="blind")
    r = post(images=[{"data": PNG}])
    check("fallback without vision → 422 vision_unsupported",
          r.status_code == 422 and r.json()["detail"] == {"error": "vision_unsupported", "provider": "claude-cli"})

    def broken(prompt, timeout, images=None):
        raise RuntimeError("down")
    api._call_gemini_fallback = broken
    r = post(images=[{"data": PNG}])
    check("both providers fail → 502", r.status_code == 502)

    # ── usage log carries image counts ──
    log = Path(os.environ["AAKA_CONFIG_DIR"]) / "logs" / "llm-usage.jsonl"
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    ok_rows = [r for r in rows if r["status"] == "ok" and r.get("images")]
    check("llm-usage.jsonl logs images + saw_images", ok_rows and ok_rows[0]["images"] == 2 and ok_rows[0]["saw_images"] == 2)
    check("llm-usage.jsonl omits image fields for text calls",
          rows[0]["status"] == "ok" and "images" not in rows[0] and "saw_images" not in rows[0])

    policy_checks(fake, gemini_calls, fake_gemini, log)

    api.subprocess.run, api.shutil.which, api._call_gemini_fallback = orig_run, orig_which, orig_gemini
    api.shutil.rmtree(os.environ["AAKA_CONFIG_DIR"], ignore_errors=True)
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all /v1/llm image checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
