"""
skills/voice.py — Aaka personality renderer.

Zero LLM. Template-based only. Singleton loaded once per process.

Usage:
    from skills.voice import get_voice
    v = get_voice()
    text = v.apply("today_schedule", raw_calendar_text, {"name": "Alice", "event_count": 3})
    text = v.apply("add_task", "", {"item": "Buy milk"})
"""
import datetime
import os
import random
import re
from pathlib import Path
from typing import Any

import yaml

VoiceContext = dict[str, Any]

_SYNC_RE = re.compile(r"^_Last synced:\s*(.+?)_$", re.MULTILINE)


def _relative_time(ts_str: str) -> str:
    """Convert 'YYYY-MM-DD HH:MM' to relative like '5 min ago'."""
    try:
        ts = datetime.datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M")
        delta = datetime.datetime.now() - ts
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return "just now"
        mins = secs // 60
        if mins < 60:
            return f"{mins} min ago"
        hrs = mins // 60
        if hrs < 24:
            return f"{hrs}h ago"
        days = hrs // 24
        return f"{days}d ago"
    except (ValueError, TypeError):
        return ts_str


def _clean_calendar(content: str) -> str:
    """Strip redundant markdown title and polish empty states."""
    # Remove "# Today — Name" or "# This Week — Name" header (voice opener covers it)
    content = re.sub(r'^#\s+.+\n', '', content)
    # Polish empty state
    content = content.replace("_No events found._", "Nothing on the calendar.")
    return content.strip()


def _extract_sync_line(content: str) -> tuple[str, str]:
    """Remove the _Last synced: ..._ line from content, return (cleaned, footer).
    Footer uses relative time and is moved to the end of the message."""
    m = _SYNC_RE.search(content)
    if not m:
        return content, ""
    rel = _relative_time(m.group(1))
    cleaned = content[:m.start()] + content[m.end():]
    # Remove any resulting blank lines at the top
    cleaned = cleaned.lstrip("\n")
    return cleaned, f"🔄 {rel}"


class Voice:
    def __init__(self, yaml_path: Path) -> None:
        self._data: dict = yaml.safe_load(yaml_path.read_text())

    # ── Public API ────────────────────────────────────────────────────────────

    def apply(self, intent: str, raw_content: str, ctx: VoiceContext) -> str:
        """
        Main entry point. Given an intent name and the raw content string,
        return the voiced version. Never raises — falls back to raw_content.
        """
        try:
            return self._apply_internal(intent, raw_content, ctx)
        except Exception:
            return raw_content

    def tone_hint(self, ctx: VoiceContext) -> str:
        """
        Return the tone_hint string with placeholders filled.
        For injection into LLM system prompts — zero tokens itself.
        """
        template = self._data.get("tone_hint", "")
        return self._fill(template, ctx)

    def quote(self) -> str:
        """Pick a random motivational quote from motivation.quotes."""
        quotes = self._data.get("motivation", {}).get("quotes", [])
        return random.choice(quotes) if quotes else ""

    # ── Internal ──────────────────────────────────────────────────────────────

    def _apply_internal(self, intent: str, raw_content: str, ctx: VoiceContext) -> str:
        if intent == "today_schedule":
            return self._apply_today(raw_content, ctx)
        if intent == "weekly_schedule":
            return self._apply_weekly(raw_content, ctx)
        if intent in ("add_event", "add_task", "complete_task"):
            return self._apply_confirmation(intent, ctx)
        # No voice layer for health_check, menu, etc.
        return raw_content

    def _apply_today(self, content: str, ctx: VoiceContext) -> str:
        sub = self._select_today_subkey(ctx)
        ctx = {**ctx, "count": ctx.get("event_count", 0)}
        opener = self._render("today_schedule", sub, ctx)
        content, sync_line = _extract_sync_line(content)
        content = _clean_calendar(content)
        footer = f"\n\n{sync_line}" if sync_line else ""
        return f"{opener}\n\n{content}{footer}"

    def _apply_weekly(self, content: str, ctx: VoiceContext) -> str:
        sub = self._select_weekly_subkey(ctx)
        ctx = {**ctx, "count": ctx.get("weekly_count", 0)}
        opener = self._render("weekly_schedule", sub, ctx)
        quote_ctx = {**ctx, "quote": self.quote()}
        closer = self._render("weekly_schedule", "closer", quote_ctx)
        content, sync_line = _extract_sync_line(content)
        content = _clean_calendar(content)
        footer = f"\n{sync_line}" if sync_line else ""
        return f"{opener}\n\n{content}\n\n{closer}{footer}"

    def _apply_confirmation(self, intent: str, ctx: VoiceContext) -> str:
        sub = self._select_confirmation_subkey(intent, ctx)
        return self._render(intent, sub, ctx)

    def _select_today_subkey(self, ctx: VoiceContext) -> str:
        if ctx.get("has_carrier"):
            return "carrier"
        count = ctx.get("event_count", 0)
        if count > 4:
            return "busy"
        if count <= 1:
            return "light"
        return "opener"

    def _select_weekly_subkey(self, ctx: VoiceContext) -> str:
        count = ctx.get("weekly_count", 0)
        if count > 12:
            return "heavy"
        if count <= 4:
            return "light"
        return "opener"

    def _select_confirmation_subkey(self, intent: str, ctx: VoiceContext) -> str:
        if intent == "add_event":
            if ctx.get("carrier"):
                return "carrier"
            return "single" if ctx.get("count", 1) == 1 else "multi"
        return ""  # flat list — _render handles missing sub-key

    def _render(self, key: str, sub: str, ctx: VoiceContext) -> str:
        """Resolve data[key][sub], pick random variant, fill placeholders."""
        node = self._data.get(key, {})
        if sub and isinstance(node, dict):
            node = node.get(sub, node.get("opener", []))
        if isinstance(node, list):
            template = random.choice(node) if node else ""
        else:
            template = str(node)
        return self._fill(template, ctx)

    @staticmethod
    def _fill(template: str, ctx: VoiceContext) -> str:
        """Format template with ctx. Missing placeholders are left intact."""
        def replacer(m: re.Match) -> str:
            val = ctx.get(m.group(1))
            return str(val) if val is not None else m.group(0)
        return re.sub(r"\{(\w+)\}", replacer, template)


# ── Singleton ─────────────────────────────────────────────────────────────────

_voice: "Voice | None" = None


def get_voice() -> Voice:
    """
    Return singleton Voice loaded from contexts/family/voice.yaml.
    Path resolved via AAKA_BASE env var (same pattern as all other modules).
    """
    global _voice
    if _voice is None:
        base = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
        yaml_path = base / "contexts" / "family" / "voice.yaml"
        _voice = Voice(yaml_path)
    return _voice
