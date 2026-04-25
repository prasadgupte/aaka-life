"""
GatewayAdapter — unified interface for zeroclaw and openclaw backends.

All outbound message sends and LLM calls route through this class.
Switch backends with the GATEWAY_BACKEND env var — no code changes needed.

Usage:
    from gateway.adapter import GatewayAdapter
    gw = GatewayAdapter()
    gw.send_message("telegram", "123456789", "Hello!")
    reply = gw.call_llm("Extract the event from: physio friday 3pm")
"""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from gateway.config import BACKEND, CLAW_BIN

_GEMINI_FALLBACK_MODELS = ["gemini-flash-latest", "gemini-2.5-flash-lite"]

# Default Claude alias for extraction-class prompts. Cheap + fast + good enough.
_CLAUDE_MODEL = os.environ.get("AAKA_CLAUDE_MODEL", "claude-haiku-4-5")


def _try_claude_cli(prompt: str, timeout: int = 60) -> str:
    """Run the local Claude CLI (subscription, zero per-call cost).

    Returns the text response. Raises RuntimeError if the CLI isn't on PATH,
    exits non-zero, or times out — callers should fall back to Gemini.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("claude not on PATH")
    result = subprocess.run(
        [claude_bin, "-p", prompt,
         "--model", _CLAUDE_MODEL,
         "--output-format", "json"],
        capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude exited {result.returncode}: {result.stderr[:300]}"
        )
    try:
        data = json.loads(result.stdout)
        return (data.get("result") or result.stdout).strip()
    except (json.JSONDecodeError, TypeError):
        return result.stdout.strip()


class GatewayAdapter:
    """Dispatches gateway + LLM calls to the configured claw backend."""

    def __init__(self, backend: str = BACKEND, claw_bin: str = CLAW_BIN):
        self.backend = backend
        self.claw_bin = claw_bin

    # ── Message sending ───────────────────────────────────────────────────────

    def send_message(
        self,
        channel: str,
        target: str,
        message: str,
        *,
        silent: bool = False,
        dry_run: bool = False,
        reply_to_message_id: str | None = None,
    ) -> None:
        """
        Send a message via the configured gateway.

        Args:
            channel:  'telegram' | 'whatsapp'
            target:   Chat ID, E.164 phone, or group JID
            message:  Text to send
            silent:   Send without notification (Telegram)
            dry_run:  Print payload without sending

        Raises RuntimeError on non-zero exit.
        """
        cmd = [
            self.claw_bin, "message", "send",
            "--channel", channel,
            "--target", target,
            "--message", message,
        ]
        if silent:
            cmd.append("--silent")
        if dry_run:
            cmd.append("--dry-run")
        if reply_to_message_id:
            cmd += ["--reply-to", str(reply_to_message_id)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"[gateway] send_message failed ({self.claw_bin}): "
                    + (result.stderr or result.stdout).strip()
                )
        except RuntimeError as exc:
            if reply_to_message_id and "--reply-to" in str(exc):
                # Graceful fallback: retry without --reply-to
                cmd_no_reply = [a for a in cmd if a not in ("--reply-to", str(reply_to_message_id))]
                result = subprocess.run(cmd_no_reply, capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(
                        f"[gateway] send_message failed ({self.claw_bin}): "
                        + (result.stderr or result.stdout).strip()
                    )
            else:
                raise

    def send_message_direct_tg(
        self,
        target: str,
        message: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        """Send a Telegram message via the egress gateway (audited, rate-limited).

        Preferred over send_message() for Telegram — avoids spawning the openclaw CLI.
        """
        from gateway.egress import send, OutboundMessage, MessageKind
        send(OutboundMessage(
            kind=MessageKind.TEXT,
            recipient=target,
            channel="telegram",
            text=message,
            source="gateway_adapter",
            reply_to_message_id=reply_to_message_id,
        ))

    def react_message(
        self,
        channel: str,
        target: str,
        message_id: str,
        emoji: str = "👀",
    ) -> None:
        """Send an emoji reaction to a message (best-effort; swallows errors)."""
        cmd = [
            self.claw_bin, "message", "react",
            "--channel", channel,
            "--target", target,
            "--message-id", str(message_id),
            "--emoji", emoji,
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception:
            pass  # non-fatal

    # ── LLM calls ─────────────────────────────────────────────────────────────

    def call_llm(self, prompt: str, timeout: int = 60) -> str:
        """
        One-shot LLM call via the configured backend.

        Returns the text response string.
        Raises RuntimeError on failure.
        """
        return self._call_llm_openclaw(prompt, timeout)

    def _call_llm_openclaw(self, prompt: str, timeout: int,
                           _gemini_only: bool = False) -> str:
        # Prefer the local Claude CLI (subscription, zero per-call cost) over
        # the Gemini API. Falls through to Gemini on any Claude failure.
        # _gemini_only=True is used by gateway.agent_api's /v1/llm fallback
        # to avoid re-entering Claude when Claude is what just failed.
        if not _gemini_only:
            try:
                text = _try_claude_cli(prompt, timeout=timeout)
                self._log_llm_usage(_CLAUDE_MODEL, "ok")
                return text
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                self._log_llm_usage(_CLAUDE_MODEL, "claude_fallback",
                                    error=str(exc)[:200])
                # Fall through to Gemini

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "LLM unavailable: Claude CLI not on PATH and GEMINI_API_KEY not set"
            )
        primary_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        models_to_try = [primary_model] + [m for m in _GEMINI_FALLBACK_MODELS if m != primary_model]

        last_exc: Exception = RuntimeError("No models to try")
        for model in models_to_try:
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
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                usage = data.get("usageMetadata", {})
                self._log_llm_usage(model, "ok",
                                    prompt_tokens=usage.get("promptTokenCount"),
                                    response_tokens=usage.get("candidatesTokenCount"))
                return text
            except urllib.error.HTTPError as exc:
                self._log_llm_usage(model, "error", error=str(exc))
                if exc.code in (503, 429):
                    last_exc = RuntimeError(
                        f"Gemini overloaded (HTTP {exc.code}) — tried {model}"
                        + (f", retrying with {models_to_try[models_to_try.index(model)+1]}" if model != models_to_try[-1] else ", all models failed. Try again in a moment.")
                    )
                    continue  # try next model
                raise RuntimeError(f"Gemini error (HTTP {exc.code}): {exc.reason}") from exc
            except Exception as exc:
                self._log_llm_usage(model, "error", error=str(exc))
                raise

        raise last_exc

    def _log_llm_usage(self, model: str, status: str, *,
                       prompt_tokens: "int | None" = None,
                       response_tokens: "int | None" = None,
                       error: str = "") -> None:
        """Append one JSON line to $AAKA_CONFIG_DIR/logs/llm-usage.jsonl."""
        import datetime
        from pathlib import Path
        config_dir = os.environ.get("AAKA_CONFIG_DIR", "/config")
        log_path = Path(config_dir) / "logs" / "llm-usage.jsonl"
        entry: dict = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "model": model,
            "status": status,
        }
        if prompt_tokens is not None:
            entry["prompt_tokens"] = prompt_tokens
        if response_tokens is not None:
            entry["response_tokens"] = response_tokens
        if error:
            entry["error"] = error
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass  # non-fatal

    # ── Gateway lifecycle ─────────────────────────────────────────────────────

    def gateway_cmd(self, *args: str) -> subprocess.CompletedProcess:
        """Run an arbitrary gateway sub-command: gateway_cmd('status')."""
        return subprocess.run(
            [self.claw_bin, "gateway", *args],
            capture_output=True, text=True,
        )

    def status(self) -> str:
        """Return gateway status string."""
        result = self.gateway_cmd("status")
        return (result.stdout or result.stderr).strip()
