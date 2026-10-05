"""Who may spend money on Gemini.

Default: nobody. An LLM call is the local Claude: the gateway's /v1/llm runs the
`claude` CLI on the subscription, and the executor goes through that gateway. A paid
API is the exception, and an exception has to be granted. There are two doors, and
each one has its own lock:

  • /v1/llm (registered agents). When Claude fails, the gateway falls back to
    Gemini only for an agent whose `agent_registry.permissions` carries
    {"gemini": {"daily_max": N}}, and at most N times per local day. Every other
    agent gets a 503 `claude_unavailable` that carries the reason.
    Grant or revoke with: admin/register_agent.py --grant-gemini <id> --daily N
                          admin/register_agent.py --revoke-gemini <id>

  • Direct calls (a process whose provider is `gemini`, i.e. the sensor, which has
    no claude). They are allowed only inside `scope(<intent>)` for an intent listed
    in AAKA_GEMINI_INTENTS (comma list, default "add_event": the live /cal
    preview). Any other caller, or none, raises GeminiNotAllowed before a request
    goes out.

When Claude reports its usage limit, the gateway pauses Claude for
AAKA_CLAUDE_LIMIT_PAUSE_MIN minutes (default 30). During the pause it answers at
once instead of starting a CLI that is going to fail, so batch callers back off
rather than hammering it. The pause and the day's counts live in
data/llm_policy.json so a gateway restart keeps them.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import json
import os
import threading
from pathlib import Path

GEMINI_INTENTS_DEFAULT = "add_event"
CLAUDE_LIMIT_PAUSE_MIN_DEFAULT = 30

# Phrases the claude CLI uses when the subscription is out of usage. Matched
# lower-case against stderr + the JSON result. Only clear subscription-limit
# messages pause Claude. A transient API 429 ("rate limit", "overloaded") does not:
# a false positive would block every agent for the whole pause.
_CLAUDE_LIMIT_MARKERS = (
    "usage limit", "limit reached", "hit your limit", "limit will reset",
    "weekly limit", "out of extra usage",
)


class GeminiNotAllowed(RuntimeError):
    """A direct Gemini call was attempted outside an allowed scope."""

    def __init__(self, scope_name: "str | None"):
        self.scope = scope_name
        where = f"'{scope_name}'" if scope_name else "this caller"
        super().__init__(
            f"Gemini is not allowed for {where} (AAKA_GEMINI_INTENTS={','.join(sorted(gemini_intents()))})")


# ── direct calls: scope allowlist ─────────────────────────────────────────────

_scope: "contextvars.ContextVar[str | None]" = contextvars.ContextVar("aaka_llm_scope", default=None)


@contextlib.contextmanager
def scope(name: str):
    """Mark the LLM calls made inside the block as made for intent `name`."""
    token = _scope.set(name)
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> "str | None":
    return _scope.get()


def gemini_intents() -> set[str]:
    raw = os.environ.get("AAKA_GEMINI_INTENTS", GEMINI_INTENTS_DEFAULT)
    return {s.strip() for s in raw.split(",") if s.strip()}


def check_direct_gemini() -> None:
    """Raise GeminiNotAllowed unless the current scope may call Gemini directly."""
    name = current_scope()
    if name is None or name not in gemini_intents():
        raise GeminiNotAllowed(name)


# ── /v1/llm: per-agent grant ──────────────────────────────────────────────────

def gemini_grant(agent: dict) -> "int | None":
    """The agent's Gemini daily_max, or None when it has no grant."""
    perms = agent.get("permissions") if hasattr(agent, "get") else None
    if isinstance(perms, str):
        try:
            perms = json.loads(perms or "{}")
        except json.JSONDecodeError:
            return None
    grant = (perms or {}).get("gemini")
    if not isinstance(grant, dict):
        return None
    try:
        daily_max = int(grant.get("daily_max", 0))
    except (TypeError, ValueError):
        return None
    return daily_max if daily_max > 0 else None


# ── shared state: day counts + Claude pause ───────────────────────────────────

_lock = threading.Lock()


def _state_path() -> Path:
    base = Path(os.environ.get("AAKA_CONFIG_DIR") or Path.home() / ".aaka")
    return base / "data" / "llm_policy.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(path)


def _today() -> str:
    return _dt.date.today().isoformat()


def gemini_take(agent_id: str, daily_max: int) -> tuple[bool, int]:
    """Count one Gemini attempt for `agent_id` today. Returns (allowed, used).
    Refused attempts are not counted."""
    with _lock:
        state = _load()
        day = state.get("gemini_used", {}).get(_today(), {})
        used = int(day.get(agent_id, 0))
        if used >= daily_max:
            return False, used
        day[agent_id] = used + 1
        state["gemini_used"] = {_today(): day}   # keep today only
        _save(state)
        return True, used + 1


def gemini_used_today() -> dict:
    return dict(_load().get("gemini_used", {}).get(_today(), {}))


def first_alert_today(key: str) -> bool:
    """True the first time `key` is raised today, so a page goes out once a day."""
    with _lock:
        state = _load()
        day = state.get("alerted", {}).get(_today(), [])
        if key in day:
            return False
        state["alerted"] = {_today(): day + [key]}
        _save(state)
        return True


def is_claude_limit(error: str) -> bool:
    low = (error or "").lower()
    return any(m in low for m in _CLAUDE_LIMIT_MARKERS)


def pause_claude(reason: str, minutes: "int | None" = None) -> str:
    """Pause Claude until now + minutes. Returns the ISO 'until' timestamp."""
    if minutes is None:
        minutes = int(os.environ.get("AAKA_CLAUDE_LIMIT_PAUSE_MIN", CLAUDE_LIMIT_PAUSE_MIN_DEFAULT))
    until = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with _lock:
        state = _load()
        state["claude_paused"] = {"until": until, "reason": (reason or "")[:300]}
        _save(state)
    return until


def claude_paused() -> "dict | None":
    """{'until', 'reason', 'retry_after'} while Claude is paused, else None."""
    p = _load().get("claude_paused")
    if not p:
        return None
    try:
        until = _dt.datetime.fromisoformat(p["until"])
    except (KeyError, ValueError):
        return None
    left = (until - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    if left <= 0:
        return None
    return {"until": p["until"], "reason": p.get("reason", ""), "retry_after": int(left) + 1}
