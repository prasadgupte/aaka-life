"""
Gateway configuration.

Fully native — no OpenClaw. Messages go through gateway.egress (Telegram HTTP,
WhatsApp via wa-sidecar, Slack Web API); LLM calls through gateway.llm_providers.

Environment variables:
  GEMINI_API_KEY    API key for the Gemini LLM provider
  LLM_PROVIDER      gemini | anthropic | claude-cli  (default: gemini) — see gateway/llm_providers.py
  ENABLED_CHANNELS  comma list, e.g. telegram,whatsapp,slack,signal (default: telegram)
"""
import os
from pathlib import Path

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

# LLM provider — direct API / local, no OpenClaw. See gateway/llm_providers.py.
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "gemini")  # gemini | anthropic | claude-cli

# Enabled channels (comma list). Default is Telegram-only. Add "whatsapp" to
# enable the Baileys wa-sidecar transport, "slack" for Slack, "signal" for the
# signal-cli JSON-RPC daemon.
# e.g. ENABLED_CHANNELS=telegram,whatsapp,slack,signal
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



# ── Signal (signal-cli JSON-RPC daemon) ───────────────────────────────────────
# Active only when "signal" ∈ ENABLED_CHANNELS. `signal-cli -a $SIGNAL_ACCOUNT
# daemon --http 127.0.0.1:18794` owns the Signal session; gateway/channels/
# signal_cli.py POSTs /api/v1/rpc and sensor/signal_poller.py consumes
# /api/v1/events (SSE). Both bind 127.0.0.1 only.
SIGNAL_CLI_URL: str = os.environ.get("SIGNAL_CLI_URL", "http://127.0.0.1:18794")
# aaka's own Signal number (+E.164). Required for a multi-account daemon, and
# used to build the signal.me invite link.
SIGNAL_ACCOUNT: str = os.environ.get("SIGNAL_ACCOUNT", "")

# Linked device vs dedicated number. True when aaka is a SECOND DEVICE on the
# operator's own Signal account (admin/setup_signal.sh link) rather than its own
# registered number. In that mode aaka sees the operator's entire Signal traffic
# and replies as them, so the router stays silent on anything that is not an
# explicit command — see router_sensor._signal_linked_mode().
SIGNAL_LINKED_MODE: bool = os.environ.get("SIGNAL_LINKED_MODE", "").strip().lower() in (
    "1", "true", "yes", "on"
)
# Optional base64 groupId of the family Signal group (see `listGroups`), the
# Signal counterpart of WHATSAPP_GROUP_JID / TELEGRAM_GROUP_ID.
SIGNAL_GROUP_ID: str = os.environ.get("SIGNAL_GROUP_ID", "")
# Where signal-cli stores received attachments (it hands us ids, not paths).
SIGNAL_ATTACHMENTS_DIR: str = os.environ.get(
    "SIGNAL_ATTACHMENTS_DIR",
    str(Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        / "signal-cli" / "attachments"),
)
# "sse" (default) streams GET /api/v1/events; "rpc" falls back to polling the
# `receive` method, for daemons built without the SSE endpoint.
SIGNAL_POLL_MODE: str = os.environ.get("SIGNAL_POLL_MODE", "sse")

# Where the signal-cli daemon runs: "sensor" (the VPS, default) or "executor"
# (the Mac). This matters because replies to QUEUED intents are not sent by the
# Mac — the executor writes them to the outbox table and sensor/flush_outbox.py,
# running from cron ON THE VPS, performs the egress send. So a channel whose
# daemon lives only on the Mac cannot answer queued intents at all (that is why
# WhatsApp is not always-on today). Default "sensor" puts signal-cli next to the
# flusher; set SIGNAL_CLI_URL to a tunnel if the daemon must live elsewhere.
SIGNAL_PLACEMENT: str = os.environ.get("SIGNAL_PLACEMENT", "sensor").strip().lower()
