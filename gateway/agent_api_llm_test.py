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


api.app.dependency_overrides[api._require_agent] = lambda: {"id": "test-agent"}
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

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        out = self.stdout_for(argv, kw)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


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
