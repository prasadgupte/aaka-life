"""
Gateway configuration.

Controls which claw backend is active (message transport) and the LLM provider.

Environment variables:
  GATEWAY_BACKEND   openclaw | zeroclaw  (default: openclaw) — message transport, being phased out
  CLAW_BIN          binary name override (default: same as GATEWAY_BACKEND)
  GEMINI_API_KEY    API key for the Gemini LLM provider
  LLM_PROVIDER      gemini | anthropic | claude-cli  (default: gemini) — see gateway/llm_providers.py
"""
import os

BACKEND: str = os.environ.get("GATEWAY_BACKEND", "openclaw")  # openclaw | zeroclaw
CLAW_BIN: str = os.environ.get("CLAW_BIN", BACKEND)           # binary name on PATH
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

# LLM provider — direct API / local, no OpenClaw. See gateway/llm_providers.py.
LLM_PROVIDER: str = os.environ.get("LLM_PROVIDER", "gemini")  # gemini | anthropic | claude-cli
