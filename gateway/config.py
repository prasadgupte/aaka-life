"""
Gateway configuration.

Controls which claw backend is active and where its binary lives.

Environment variables:
  GATEWAY_BACKEND   openclaw | zeroclaw  (default: openclaw)
  CLAW_BIN          binary name override (default: same as GATEWAY_BACKEND)
  GEMINI_API_KEY    API key for Gemini LLM (used by zeroclaw backend)
"""
import os

BACKEND: str = os.environ.get("GATEWAY_BACKEND", "openclaw")  # openclaw | zeroclaw
CLAW_BIN: str = os.environ.get("CLAW_BIN", BACKEND)           # binary name on PATH
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
