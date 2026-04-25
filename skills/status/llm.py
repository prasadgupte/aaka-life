"""
LLM usage status — reads llm-usage.jsonl and optionally pings the Gemini API.
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path


def status_llm(ping: bool = True) -> str:
    config_dir = os.environ.get("AAKA_CONFIG_DIR", "/config")
    logs_dir = Path(config_dir) / "logs"
    usage_file = logs_dir / "llm-usage.jsonl"

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    key_status = "set" if api_key else "NOT SET"

    lines = [
        "LLM status",
        "──────────",
        f"Model  : {model}",
        f"API key: {key_status}",
    ]

    # Parse usage log
    if usage_file.exists():
        entries = []
        for raw in usage_file.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                pass

        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        week_ago = now - timedelta(days=7)

        today_ok = today_err = 0
        today_prompt = today_resp = 0
        week_ok = week_err = 0
        week_prompt = week_resp = 0
        last_ts = None

        for e in entries:
            ts_str = e.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if last_ts is None or ts > last_ts:
                last_ts = ts

            ok = e.get("status") == "ok"
            pt = e.get("prompt_tokens", 0) or 0
            rt = e.get("response_tokens", 0) or 0

            if ts_str.startswith(today_str):
                if ok:
                    today_ok += 1
                    today_prompt += pt
                    today_resp += rt
                else:
                    today_err += 1

            if ts >= week_ago:
                if ok:
                    week_ok += 1
                    week_prompt += pt
                    week_resp += rt
                else:
                    week_err += 1

        lines.append("")
        lines.append(f"Today   : {today_ok} ok, {today_err} err  |  {today_prompt} prompt + {today_resp} resp tokens")
        lines.append(f"7 days  : {week_ok} ok, {week_err} err  |  {week_prompt} prompt + {week_resp} resp tokens")
        lines.append(f"Last call: {last_ts.strftime('%Y-%m-%d %H:%M UTC') if last_ts else '(none)'}")
    else:
        lines.append("")
        lines.append("(no llm-usage.jsonl found — log file will be created on first LLM call)")

    # Heartbeat ping
    lines.append("")
    if not api_key:
        lines.append("Heartbeat: skipped (no API key)")
    elif not ping:
        lines.append("Heartbeat: skipped")
    else:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        body = json.dumps({
            "contents": [{"parts": [{"text": "ping"}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 4},
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            latency_ms = int((time.monotonic() - t0) * 1000)
            lines.append(f"Heartbeat: ✅ {latency_ms}ms")
        except Exception as exc:
            lines.append(f"Heartbeat: ❌ {exc}")

    return "\n".join(lines)
