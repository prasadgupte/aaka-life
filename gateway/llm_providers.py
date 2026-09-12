#!/usr/bin/env python3
"""
gateway/llm_providers.py — pluggable, provider-agnostic LLM backends.

Each provider is a function `<name>(prompt, timeout, images=None) -> str` that
raises RuntimeError on failure. `complete()` dispatches on LLM_PROVIDER (default: gemini).

`images` is an optional list of `{"data": <base64>, "media_type": "image/png"|"image/jpeg",
"label": str}` sent alongside the prompt. gemini and anthropic pass them inline; claude-cli
cannot (a one-shot `-p` has no image input) and raises VisionUnsupported so the caller
never gets a text-only answer pretending it saw the picture.

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

Images = "list[dict] | None"


class VisionUnsupported(RuntimeError):
    """The selected provider cannot take images; raised before any call is made."""

    def __init__(self, provider: str):
        super().__init__(f"provider '{provider}' does not support images")
        self.provider = provider


def _image_label(i: int, img: dict) -> str:
    label = (img.get("label") or "").strip()
    return f"Image {i} ({label}):" if label else f"Image {i}:"


def provider_name(explicit: "str | None" = None) -> str:
    return explicit or os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER)


def complete(prompt: str, timeout: int = 60, provider: "str | None" = None,
             images: Images = None) -> str:
    """Dispatch a one-shot completion to the selected provider. Returns text."""
    name = provider_name(provider)
    fn = _PROVIDERS.get(name)
    if fn is None:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER '{name}'. Options: {', '.join(sorted(_PROVIDERS))}"
        )
    if images and name not in _VISION_PROVIDERS:
        raise VisionUnsupported(name)
    return fn(prompt, timeout, images)


# ── Gemini ──────────────────────────────────────────────────────────────────

def gemini(prompt: str, timeout: int = 60, images: Images = None) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    primary = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    models = [primary] + [m for m in GEMINI_FALLBACK_MODELS if m != primary]

    # Images go first, each preceded by its label so the prompt can refer to
    # "Image 2 (option C)" the same way it does on the claude-cli path.
    parts: list[dict] = []
    for i, img in enumerate(images or [], 1):
        parts.append({"text": _image_label(i, img)})
        parts.append({"inline_data": {"mime_type": img.get("media_type", "image/png"),
                                      "data": img["data"]}})
    parts.append({"text": prompt})

    last_exc: Exception = RuntimeError("No models to try")
    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        body = json.dumps({
            "contents": [{"parts": parts}],
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

def anthropic(prompt: str, timeout: int = 60, images: Images = None) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    url = "https://api.anthropic.com/v1/messages"
    content: "str | list[dict]" = prompt
    if images:
        content = []
        for i, img in enumerate(images, 1):
            content.append({"type": "text", "text": _image_label(i, img)})
            content.append({"type": "image", "source": {
                "type": "base64",
                "media_type": img.get("media_type", "image/png"),
                "data": img["data"]}})
        content.append({"type": "text", "text": prompt})
    body = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "temperature": 0,
        "messages": [{"role": "user", "content": content}],
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

def claude_cli(prompt: str, timeout: int = 60, images: Images = None) -> str:
    if images:
        raise VisionUnsupported("claude-cli")
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
_VISION_PROVIDERS = {"gemini", "anthropic"}


if __name__ == "__main__":
    import sys
    p = " ".join(sys.argv[1:]) if sys.argv[1:] else sys.stdin.read().strip()
    print(complete(p))
