"""
GatewayAdapter — unified interface for outbound messages and LLM calls.

Everything is native now: messages go through gateway.egress (Telegram HTTP,
WhatsApp via the wa-sidecar, Slack Web API) and LLM calls go through
gateway.llm_providers (gemini · anthropic · claude-cli). No OpenClaw, no claw
subprocess — the abstraction that once dispatched to a claw binary is gone.

Usage:
    from gateway.adapter import GatewayAdapter
    gw = GatewayAdapter()
    gw.send_message("telegram", "123456789", "Hello!")
    reply = gw.call_llm("Extract the event from: physio friday 3pm")
"""

import json
import os


class GatewayAdapter:
    """Dispatches outbound messages (via egress) and LLM calls (via providers)."""

    def __init__(self, *_ignored, **_kwignored):
        # Positional/keyword args are accepted and ignored for back-compat with
        # old GatewayAdapter(backend=..., claw_bin=...) call sites.
        pass

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
        Send a message via the native egress gateway.

        Args:
            channel:  'telegram' | 'whatsapp' | 'slack'
            target:   Chat ID, E.164 phone, or group JID
            message:  Text to send
            silent:   Send without notification (Telegram)
            dry_run:  Print payload without sending
        """
        if dry_run:
            print(f"[dry-run] {channel} → {target}: {message[:80]}")
            return

        from gateway.egress import send, OutboundMessage, MessageKind
        send(OutboundMessage(
            kind=MessageKind.TEXT,
            recipient=target,
            channel=channel,
            text=message,
            source="gateway_adapter",
            silent=silent,
            reply_to_message_id=reply_to_message_id,
        ))

    def send_message_direct_tg(
        self,
        target: str,
        message: str,
        *,
        silent: bool = False,
        reply_to_message_id: str | None = None,
        bot_id: str | None = None,
    ) -> None:
        """Send a Telegram message via egress. `bot_id` selects which bot token to
        use for multi-bot deployments (see gateway/channels/telegram.py)."""
        from gateway.egress import send, OutboundMessage, MessageKind
        send(OutboundMessage(
            kind=MessageKind.TEXT,
            recipient=target,
            channel="telegram",
            text=message,
            source="gateway_adapter",
            silent=silent,
            reply_to_message_id=reply_to_message_id,
            bot_id=bot_id,
        ))

    def react_message(
        self,
        channel: str,
        target: str,
        message_id: str,
        emoji: str = "👀",
    ) -> None:
        """Send an emoji reaction to a message (best-effort; swallows errors)."""
        try:
            from gateway.egress import reaction, send
            send(reaction(
                recipient=target, channel=channel, emoji=emoji,
                message_id=str(message_id), source="gateway_adapter",
            ))
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

    def _call_llm_fallback(self, prompt: str, timeout: int,
                           _gemini_only: bool = False) -> str:
        """Fallback LLM entry used by gateway.agent_api's /v1/llm route.
        _gemini_only=True forces the Gemini provider."""
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
