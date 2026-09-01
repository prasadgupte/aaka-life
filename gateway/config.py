"""
Gateway configuration.

Fully native — no OpenClaw. Messages go through gateway.egress (Telegram HTTP,
WhatsApp via wa-sidecar, Slack Web API); LLM calls through gateway.llm_providers.

Environment variables:
  GEMINI_API_KEY    API key for the Gemini LLM provider
  LLM_PROVIDER      gemini | anthropic | claude-cli  (default: gemini) — see gateway/llm_providers.py
  ENABLED_CHANNELS  comma list, e.g. telegram,whatsapp,slack (default: telegram)
"""
import os
from pathlib import Path

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

# LLM provider — direct API / local, no OpenClaw. See gateway/llm_providers.py.
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "gemini")  # gemini | anthropic | claude-cli

# Enabled channels (comma list). Default is Telegram-only. Add "whatsapp" to
# enable the Baileys wa-sidecar transport, "slack" for Slack.
# e.g. ENABLED_CHANNELS=telegram,whatsapp,slack
ENABLED_CHANNELS: list = [
    c.strip() for c in os.environ.get("ENABLED_CHANNELS", "telegram").split(",") if c.strip()
]

# ── wa-sidecar (Baileys WhatsApp, OpenClaw-free) ──────────────────────────────
# Active only when "whatsapp" ∈ ENABLED_CHANNELS. The Node sidecar owns the
# WhatsApp Web session; gateway/channels/whatsapp.py POSTs it on WA_SIDECAR_PORT,
# and sensor/wa_inbound.py receives inbound POSTs on WA_RECEIVER_PORT. Both bind
# 127.0.0.1 only. WA_AUTH_DIR is read by the sidecar (surfaced here for diagnose).
WA_SIDECAR_PORT: int = int(os.environ.get("WA_SIDECAR_PORT", "18792"))
WA_RECEIVER_PORT: int = int(os.environ.get("WA_RECEIVER_PORT", "18793"))
WA_AUTH_DIR: str = os.environ.get(
    "WA_AUTH_DIR",
    str(Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "whatsapp-auth"),
)

