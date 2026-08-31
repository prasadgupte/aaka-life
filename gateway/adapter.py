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

# LLM providers now live in gateway/llm_providers.py (gemini · anthropic · claude-cli),
# selected by LLM_PROVIDER. call_llm() below delegates there.


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

    def call_llm(self, prompt: str, timeout: int = 60,
                 provider: "str | None" = None) -> str:
        """
        One-shot LLM call via the selected provider (LLM_PROVIDER, default: gemini).

        Providers are direct API / local — no OpenClaw. See gateway/llm_providers.py.
        Returns the text response. Raises RuntimeError on failure.
        """
        from gateway import llm_providers
        name = llm_providers.provider_name(provider)
        try:
            text = llm_providers.complete(prompt, timeout, provider=name)
            self._log_llm_usage(name, "ok")
            return text
        except Exception as exc:
            self._log_llm_usage(name, "error", error=str(exc)[:200])
            raise

    def _call_llm_openclaw(self, prompt: str, timeout: int,
                           _gemini_only: bool = False) -> str:
        # Back-compat shim. gateway.agent_api's /v1/llm fallback passes
        # _gemini_only=True to force the Gemini provider.
        return self.call_llm(prompt, timeout,
                             provider="gemini" if _gemini_only else None)

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
