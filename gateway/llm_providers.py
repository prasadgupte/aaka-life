#!/usr/bin/env python3
"""
gateway/llm_providers.py — pluggable, provider-agnostic LLM backends.

Each provider is a function `<name>(prompt, timeout) -> str` that raises
RuntimeError on failure. `complete()` dispatches on LLM_PROVIDER (default: gemini).

Providers (all direct API / local — no OpenClaw):
  gemini      Google Gemini API      (GEMINI_API_KEY)      ← default, easy free on-ramp
  anthropic   Anthropic Messages API (ANTHROPIC_API_KEY)   ← first-class Claude
  claude-cli  local `claude` binary  (subscription CLI)    ← optional, for CLI users

Env:
  LLM_PROVIDER      gemini | anthropic | claude-cli   (default: gemini)
  GEMINI_API_KEY / GEMINI_MODEL
  ANTHROPIC_API_KEY / ANTHROPIC_MODEL
  AAKA_CLAUDE_MODEL (claude-cli model)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

DEFAULT_PROVIDER = "gemini"
GEMINI_FALLBACK_MODELS = ["gemini-flash-latest", "gemini-2.5-flash-lite"]


def provider_name(explicit: "str | None" = None) -> str:
    return explicit or os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER)


def complete(prompt: str, timeout: int = 60, provider: "str | None" = None) -> str:
    """Dispatch a one-shot completion to the selected provider. Returns text."""
    name = provider_name(provider)
    fn = _PROVIDERS.get(name)
    if fn is None:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER '{name}'. Options: {', '.join(sorted(_PROVIDERS))}"
        )
    return fn(prompt, timeout)


# ── Gemini ──────────────────────────────────────────────────────────────────

def gemini(prompt: str, timeout: int = 60) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    primary = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    models = [primary] + [m for m in GEMINI_FALLBACK_MODELS if m != primary]

    last_exc: Exception = RuntimeError("No models to try")
    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0},
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as exc:
            if exc.code in (503, 429):
                last_exc = RuntimeError(f"Gemini overloaded (HTTP {exc.code}) on {model}")
                continue  # try next model
            raise RuntimeError(f"Gemini error (HTTP {exc.code}): {exc.reason}") from exc
    raise last_exc


# ── Anthropic (Claude API) ──────────────────────────────────────────────────

def anthropic(prompt: str, timeout: int = 60) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    url = "https://api.anthropic.com/v1/messages"
    body = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            pass
        raise RuntimeError(f"Anthropic error (HTTP {exc.code}): {exc.reason} {detail}") from exc
    parts = data.get("content", [])
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return text.strip()


# ── Local Claude CLI (optional, subscription) ───────────────────────────────

def claude_cli(prompt: str, timeout: int = 60) -> str:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude not on PATH")
    model = os.environ.get("AAKA_CLAUDE_MODEL", "claude-haiku-4-5")
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", model, "--output-format", "json"],
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:300]}")
    try:
        data = json.loads(result.stdout)
        return (data.get("result") or result.stdout).strip()
    except (json.JSONDecodeError, TypeError):
        return result.stdout.strip()


_PROVIDERS = {
    "gemini": gemini,
    "anthropic": anthropic,
    "claude-cli": claude_cli,
}


if __name__ == "__main__":
    import sys
    p = " ".join(sys.argv[1:]) if sys.argv[1:] else sys.stdin.read().strip()
    print(complete(p))
