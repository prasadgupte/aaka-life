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
  gemini      Google Gemini API      (GEMINI_API_KEY)      ← default on the sensor, easy free on-ramp
  anthropic   Anthropic Messages API (ANTHROPIC_API_KEY)   ← first-class Claude
  claude-cli  local `claude` binary  (subscription CLI)    ← optional, for CLI users
  gateway     aaka's own /v1/llm on the agent gateway      ← default on the executor (home):
              one HTTP hop to localhost:18790, Haiku via the local `claude`, Gemini only
              as the gateway's own last resort; every call lands in the gateway's usage log.

Env:
  LLM_PROVIDER      gemini | anthropic | claude-cli | gateway
                    (default: gateway when AAKA_ROLE is home/executor, else gemini)
  GEMINI_API_KEY / GEMINI_MODEL
  ANTHROPIC_API_KEY / ANTHROPIC_MODEL
  AAKA_CLAUDE_MODEL (claude-cli model)
  AAKA_LLM_GATEWAY_KEY   X-Agent-Key for the gateway provider (a registered agent, e.g. `executor`)
  AAKA_LLM_GATEWAY_URL   default http://127.0.0.1:$AGENT_API_PORT/v1/llm
  AAKA_LLM_GATEWAY_COMPLEXITY  low (haiku) | medium (sonnet) | high (opus) — default low
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_PROVIDER = "gemini"
GEMINI_FALLBACK_MODELS = ["gemini-flash-latest", "gemini-2.5-flash-lite"]
# A 429/503 on every model in one pass means the whole free-tier quota is
# hobbled, not one model — cycling models again instantly just repeats the
# failure. Wait, then retry the full list. (2026-09-19: a /cal add hit "HTTP
# 429 on gemini-2.5-flash-lite" — the LAST fallback — meaning all three had
# already failed once with zero delay between them.)
GEMINI_RETRY_BACKOFF_SECONDS = [2, 5]  # sleep before each retry pass (2 retries = 3 passes total)

Images = "list[dict] | None"


class LLMBudgetExceeded(RuntimeError):
    """The provider refused for a reason that will not clear by retrying now:
    a monthly spend cap or a daily quota. Callers that can hand the job to
    another machine/provider should, instead of backing off."""

    def __init__(self, provider: str, detail: str):
        super().__init__(f"{provider} budget exhausted — {detail}")
        self.provider = provider
        self.detail = detail


# Substrings of a Gemini 429 body that mean "come back tomorrow/next month",
# not "try again in a few seconds". Matched case-insensitively.
_GEMINI_BUDGET_MARKERS = ("spending cap", "spend cap", "perday", "per day", "daily")


class VisionUnsupported(RuntimeError):
    """The selected provider cannot take images; raised before any call is made."""

    def __init__(self, provider: str):
        super().__init__(f"provider '{provider}' does not support images")
        self.provider = provider


def _image_label(i: int, img: dict) -> str:
    label = (img.get("label") or "").strip()
    return f"Image {i} ({label}):" if label else f"Image {i}:"


def provider_name(explicit: "str | None" = None) -> str:
    """Which provider a call uses: an explicit argument, else LLM_PROVIDER,
    else the role default — the executor (home) always goes through aaka's
    own gateway (Haiku), the sensor (away) talks to Gemini directly."""
    if explicit:
        return explicit
    configured = os.environ.get("LLM_PROVIDER", "").strip()
    if configured:
        return configured
    import aaka_config
    return "gateway" if aaka_config.role() == "home" else DEFAULT_PROVIDER


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
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0},
    }).encode()

    last_exc: Exception = RuntimeError("No models to try")
    passes = len(GEMINI_RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(passes):
        if attempt > 0:
            time.sleep(GEMINI_RETRY_BACKOFF_SECONDS[attempt - 1])
        for model in models:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
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
                    detail = _gemini_error_message(exc)
                    if exc.code == 429 and _is_budget_error(detail):
                        # A spend cap / daily quota: every model and every pass
                        # will say the same thing — don't burn 9 calls finding out.
                        raise LLMBudgetExceeded("Gemini", detail) from exc
                    last_exc = RuntimeError(
                        f"Gemini overloaded (HTTP {exc.code}) on {model}"
                        f" (pass {attempt + 1}/{passes})")
                    continue  # try next model this pass
                raise RuntimeError(f"Gemini error (HTTP {exc.code}): {exc.reason}") from exc
    raise last_exc


def _gemini_error_message(exc: "urllib.error.HTTPError") -> str:
    """The human message inside a Gemini error body ({"error": {"message": …}}),
    or the HTTP reason if the body isn't the usual shape."""
    try:
        body = exc.read().decode("utf-8", "replace")
        return (json.loads(body).get("error") or {}).get("message") or exc.reason or ""
    except Exception:
        return str(exc.reason or "")


def _is_budget_error(detail: str) -> bool:
    low = (detail or "").lower()
    return any(m in low for m in _GEMINI_BUDGET_MARKERS)


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

# Where a `claude` binary lives when the process was started by launchd/cron
# with the bare system PATH (the executor's queue worker, the tool runner).
_CLAUDE_CANDIDATES = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                      "~/.local/bin/claude", "~/.claude/local/claude")


def find_claude() -> "str | None":
    """Path of a local `claude` binary — PATH first, then the usual install
    spots — or None. The executor runs under launchd with a minimal PATH, so
    `shutil.which` alone reported "claude not on PATH" on a Mac that has it."""
    found = shutil.which("claude")
    if found:
        return found
    for cand in _CLAUDE_CANDIDATES:
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def claude_cli(prompt: str, timeout: int = 60, images: Images = None) -> str:
    if images:
        raise VisionUnsupported("claude-cli")
    claude_bin = find_claude()
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


# ── aaka gateway (/v1/llm) ─────────────────────────────────────────────────

def gateway_url() -> str:
    return os.environ.get("AAKA_LLM_GATEWAY_URL") or \
        f"http://127.0.0.1:{os.environ.get('AGENT_API_PORT', '18790')}/v1/llm"


def gateway(prompt: str, timeout: int = 60, images: Images = None) -> str:
    """aaka's own LLM endpoint (gateway/agent_api.py → POST /v1/llm).

    The executor never talks to a model vendor itself: the gateway runs Haiku
    through the local `claude` (complexity "low"), falls back to Gemini on its
    own terms, and logs every call in one place. Needs AAKA_LLM_GATEWAY_KEY —
    the X-Agent-Key of a registered agent (`admin/register_agent.py executor`).
    Images are passed through; the gateway decides whether it can see them."""
    key = os.environ.get("AAKA_LLM_GATEWAY_KEY", "").strip()
    if not key:
        raise RuntimeError("AAKA_LLM_GATEWAY_KEY not set — register an agent for the executor "
                           "(admin/register_agent.py executor) and put its key in .env")
    body: dict = {
        "prompt": prompt,
        "response_format": "text",
        "complexity": os.environ.get("AAKA_LLM_GATEWAY_COMPLEXITY", "low"),
    }
    if images:
        body["images"] = [{"data": img["data"], "media_type": img.get("media_type", "image/png"),
                           "label": img.get("label", "")} for img in images]
    req = urllib.request.Request(
        gateway_url(), data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Agent-Key": key},
    )
    # Outlast the endpoint's worst case (claude 120 s / 180 s with images, then
    # its own 60 s Gemini rescue) so a slow run isn't cut off client-side while
    # the gateway completes and logs "ok".
    floor = 250 if images else 190
    try:
        with urllib.request.urlopen(req, timeout=max(timeout, floor)) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        if exc.code == 401:
            raise RuntimeError("aaka gateway rejected AAKA_LLM_GATEWAY_KEY (401) — re-register the executor agent") from exc
        if exc.code == 422 and "vision_unsupported" in detail:
            raise VisionUnsupported("gateway") from exc
        raise RuntimeError(f"aaka gateway /v1/llm HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"aaka gateway unreachable at {gateway_url()}: {exc.reason} "
                           "(is com.aaka.agentapi running?)") from exc
    text = data.get("text")
    if not isinstance(text, str):
        raise RuntimeError(f"aaka gateway returned no text: {str(data)[:200]}")
    return text


_PROVIDERS = {
    "gemini": gemini,
    "anthropic": anthropic,
    "claude-cli": claude_cli,
    "gateway": gateway,
}


def local_fallback_provider(failed: str, images: Images = None) -> "str | None":
    """A provider that can stand in when `failed` is out of budget, or None.

    Only `claude-cli` qualifies: it bills against a subscription, not the API
    key that just ran dry, and it exists only where a `claude` binary is
    installed — so this is a no-op on a bare sensor and a real rescue on the
    executor. It cannot take images."""
    if failed in ("claude-cli", "gateway") or images:
        # gateway: it already ran the local claude itself — nothing local left to try
        return None
    if os.environ.get("AAKA_LLM_LOCAL_FALLBACK", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    return "claude-cli" if find_claude() else None
_VISION_PROVIDERS = {"gemini", "anthropic", "gateway"}  # gateway: it decides, raises VisionUnsupported itself


if __name__ == "__main__":
    import sys
    p = " ".join(sys.argv[1:]) if sys.argv[1:] else sys.stdin.read().strip()
    print(complete(p))
