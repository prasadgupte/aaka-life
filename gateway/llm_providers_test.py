#!/usr/bin/env python3
"""
gateway/llm_providers_test.py — mock-first unit tests (no network).

Monkeypatches urllib.request.urlopen to assert each provider builds the correct
request (URL, headers, body) and parses the response, plus dispatch + errors.
Run: python3 gateway/llm_providers_test.py   (exit 0 = pass)
"""
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway import llm_providers as lp  # noqa: E402

_FAILURES = []


def check(desc, cond):
    print(("  ok  " if cond else " FAIL ") + desc)
    if not cond:
        _FAILURES.append(desc)


class _FakeResp:
    def __init__(self, payload): self._b = json.dumps(payload).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _patch(capture, payload):
    """Replace urlopen: records the Request into `capture`, returns `payload`."""
    def fake_urlopen(req, timeout=None):
        capture["url"] = req.full_url
        capture["method"] = req.get_method()
        capture["headers"] = {k.lower(): v for k, v in req.header_items()}
        capture["body"] = json.loads(req.data.decode()) if req.data else None
        capture["timeout"] = timeout
        return _FakeResp(payload)
    urllib.request.urlopen = fake_urlopen


def main():
    orig = urllib.request.urlopen

    # ── Gemini ──
    os.environ["GEMINI_API_KEY"] = "test-gem-key"
    os.environ.pop("GEMINI_MODEL", None)
    cap = {}
    _patch(cap, {"candidates": [{"content": {"parts": [{"text": "hi from gemini"}]}}]})
    out = lp.gemini("hello", timeout=9)
    check("gemini: returns parsed text", out == "hi from gemini")
    check("gemini: hits generativelanguage endpoint", "generativelanguage.googleapis.com" in cap["url"])
    check("gemini: key in query string", "key=test-gem-key" in cap["url"])
    check("gemini: default model gemini-2.5-flash", "gemini-2.5-flash:generateContent" in cap["url"])
    check("gemini: POST", cap["method"] == "POST")
    check("gemini: prompt in body", cap["body"]["contents"][0]["parts"][0]["text"] == "hello")
    check("gemini: temperature 0", cap["body"]["generationConfig"]["temperature"] == 0)

    # ── Anthropic ──
    os.environ["ANTHROPIC_API_KEY"] = "test-ant-key"
    os.environ.pop("ANTHROPIC_MODEL", None)
    cap = {}
    _patch(cap, {"content": [{"type": "text", "text": "hi from claude"}]})
    out = lp.anthropic("hello", timeout=9)
    check("anthropic: returns parsed text", out == "hi from claude")
    check("anthropic: hits api.anthropic.com/v1/messages", cap["url"] == "https://api.anthropic.com/v1/messages")
    check("anthropic: x-api-key header", cap["headers"].get("x-api-key") == "test-ant-key")
    check("anthropic: anthropic-version header", cap["headers"].get("anthropic-version") == "2023-06-01")
    check("anthropic: default model claude-haiku-4-5", cap["body"]["model"] == "claude-haiku-4-5")
    check("anthropic: message role/content", cap["body"]["messages"][0] == {"role": "user", "content": "hello"})

    # ── Dispatch ──
    os.environ["LLM_PROVIDER"] = "anthropic"
    check("complete(): honors LLM_PROVIDER", lp.provider_name() == "anthropic")
    check("complete(): explicit provider overrides env", lp.provider_name("gemini") == "gemini")
    _patch({}, {"content": [{"type": "text", "text": "dispatched"}]})
    check("complete(): dispatches to selected provider", lp.complete("x", provider="anthropic") == "dispatched")

    # ── Errors ──
    try:
        lp.complete("x", provider="nope")
        check("unknown provider raises", False)
    except RuntimeError as e:
        check("unknown provider raises", "Unknown LLM_PROVIDER" in str(e))

    os.environ.pop("GEMINI_API_KEY", None)
    try:
        lp.gemini("x")
        check("missing GEMINI_API_KEY raises", False)
    except RuntimeError as e:
        check("missing GEMINI_API_KEY raises", "GEMINI_API_KEY not set" in str(e))

    urllib.request.urlopen = orig
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s)")
        return 1
    print("all llm-provider checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
