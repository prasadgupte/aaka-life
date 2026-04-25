#!/usr/bin/env python3
"""
Shared LLM helper — all calls routed through gateway/adapter.py.

GATEWAY_BACKEND controls which backend is active:
  zeroclaw  → Gemini via ZeroClaw (default)
  openclaw  → openclaw agent 'llm'
"""
import sys
from gateway.adapter import GatewayAdapter

_adapter = GatewayAdapter()


def call_llm(prompt: str, timeout: int = 60) -> str:
    """One-shot LLM call via the configured gateway. Raises RuntimeError on failure."""
    return _adapter.call_llm(prompt, timeout=timeout)


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) if sys.argv[1:] else sys.stdin.read().strip()
    print(call_llm(prompt))
