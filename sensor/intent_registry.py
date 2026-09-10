"""
sensor/intent_registry.py — intent pattern matching and local-intent dispatch.

Provides:
  match_intent(message_lower)  → intent name or None
  dispatch_local(intent, message, sender, channel_id, source) → str

dispatch_local checks skills/registry.yaml `enabled` flag before calling
the appropriate domain handler.  Intents not in the registry are always
considered enabled.
"""
import re
import os
from functools import lru_cache
from pathlib import Path
from typing import Callable

# ── Intent pattern table ──────────────────────────────────────────────────────
# (intent_name, [regex_patterns])  — first match wins, patterns tested
# against the lower-cased message.

INTENT_PATTERNS: list[tuple[str, list[str]]] = [
    ("llm_call",       [r"^/llm\b"]),
    ("flush_outbox",   [r"^/outbox\b"]),
    ("queue_test",     [r"^/tqueue\b"]),
    ("executor_echo",  [r"^/texec\b"]),
    ("test_thread",    [r"^/tthread\b"]),
    ("test_status",    [r"^/tstatus\b"]),
    ("route_tags",     [r"^/route\s+"]),
    ("list_tags",      [r"^/tags\b"]),
    ("tag_manage",     [r"^/tag\b"]),
    ("read_note",      [r"^/notes\b", r"^notes\b"]),
    ("drop_note",      [r"^/note\s+"]),
    ("drop_file",      [r"^/drop\b"]),
    ("buy_list",       [r"^/buy\b", r"^#done$", r"^#clear$", r"^#all$", r"^#share$", r"^#private$"]),
    ("edit_event",     [r"^/cal\s+edit\b", r"^/edit\s+event\b"]),
    ("fix_event",      [r"^/cal\s+fix\b", r"^/fix\b", r"^/cal\s+#fix\b"]),
    ("add_event",      [r"^/cal\b", r"^/add_event\b", r"^/add\b", r"\badd\s+(event|appointment|meeting)\b"]),
    ("plan_slots",     [r"^/plan\b", r"^plan\b"]),
    ("day_schedule",   [r"^/day\b", r"^day\s+\S"]),
    ("block_cal",      [r"^/block\b", r"^block\b"]),
    ("add_task",       [r"^/addtask\b", r"^/task\b", r"\badd\s+task\b", r"\bnew\s+task\b",
                        r"\bremind\s+me\s+to\b"]),
    ("invite",         [r"^/invite\b"]),
    ("tools_list",     [r"^/tools\b"]),
    ("mcp_view",       [r"^/mcp\b"]),
    ("security_audit", [r"^/security\b"]),
    ("secure_scan",    [r"^/secure\b"]),
    ("agent_dispatch", [r"^/ask\b"]),
    ("list_tasks",     [r"^/tasks\b"]),
    ("edit_task",      [r"^/edit\b"]),
    ("delete_task",    [r"^/del\b", r"^/delete\b"]),
    ("snooze_task",    [r"^/snooze\b"]),
    ("complete_task",  [r"^/done\b", r"^/complete\b", r"\bmark\s+.+\s+done\b", r"\bfinish\s+task\b"]),
    ("today_schedule", [r"^/today\b", r"\btoday\b", r"\bschedule\b",
                        r"\bwhat('s| is) (on |happening )?(today|tonight)\b"]),
    ("weekly_schedule",[r"^/week\b", r"\bweek\b", r"\bthis week\b", r"\bnext \d+ days\b",
                        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"]),
    ("health_check",   [r"\bstatus\b", r"\bhealth\b", r"\blast sync\b", r"\baudit\b"]),
    ("menu",           [r"^/menu\b", r"\bcommands?\b"]),
    ("members_list",   [r"^/members\b", r"^members$"]),
    ("birthday_list",  [r"^/bday\b", r"^bday\b"]),
    ("bday_wish",      [r"^bday\s+wish\b", r"^bday\s+send\b", r"^bday\s+msg\b"]),
    ("engage_report",  [r"^/engage\b", r"^engage\b"]),
    ("budget",         [r"^/expense\b", r"^/budget\b"]),
    ("undo_queue",     [r"^/undo\b", r"^undo\s+#"]),
    ("keep_original",  [r"^/keep\s+#", r"^keep\s+#"]),
    ("confirm_approval", [r"^/confirm\b", r"^confirm\s+#"]),
    ("pdf_tool",       [r"^/pdf\b", r"^pdf\s+"]),
    ("pay",            [r"^/pay\b", r"^pay\s+"]),
    ("approve_sender", [r"^/approve\s+\S+"]),
    ("deny_sender",    [r"^/deny\s+\S+"]),
    ("errors_report",  [r"^/errors\b"]),
    ("linkedin_post",  [r"^/li\s+", r"^/linkedin\s+"]),
    ("mail_fetch",     [r"^/mail\s+fetch\b"]),
    ("mail_view",      [r"^/mail\b"]),
    ("pw_manage",      [r"^/pw\b", r"^/pass\b"]),
]

# Compiled once
_COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (intent, [re.compile(p) for p in patterns])
    for intent, patterns in INTENT_PATTERNS
]


def match_intent(message_lower: str) -> "str | None":
    """Return the first matching intent name, or None."""
    for intent, patterns in _COMPILED:
        for pat in patterns:
            if pat.search(message_lower):
                return intent
    return None


# ── Enabled-intent check ──────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_registry() -> dict:
    try:
        import yaml as _yaml
        p = Path(__file__).resolve().parent.parent / "skills" / "registry.yaml"
        if p.exists():
            data = _yaml.safe_load(p.read_text()) or {}
            return data.get("skills", {})
    except Exception:
        pass
    return {}


def _is_enabled(intent: str) -> bool:
    """Return True if the intent is enabled in skills/registry.yaml, or not registered there."""
    registry = _load_registry()
    # Find a skill whose `intent` field matches
    for skill_cfg in registry.values():
        if isinstance(skill_cfg, dict) and skill_cfg.get("intent") == intent:
            return bool(skill_cfg.get("enabled", True))
    return True  # not in registry → always enabled


# ── Domain handler table ──────────────────────────────────────────────────────

# Imported lazily to keep module-level import cost low.
_DOMAIN_MODULES = [
    "sensor.intents.calendar",
    "sensor.intents.tasks",
    "sensor.intents.notes",
    "sensor.intents.tags",
    "sensor.intents.admin",
    "sensor.intents.engagement",
    "sensor.intents.lists",
    "sensor.intents.misc",
]

_DISPATCH_TABLE: "dict[str, Callable] | None" = None


def _build_dispatch_table() -> "dict[str, Callable]":
    import importlib
    table: dict[str, Callable] = {}
    for mod_name in _DOMAIN_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            for intent in mod.HANDLES:
                table[intent] = mod.handle
        except Exception as exc:
            import logging
            logging.getLogger("intent_registry").warning("failed to load %s: %s", mod_name, exc)
    return table


def dispatch_local(
    intent: str,
    message: str,
    sender: str,
    channel_id: str,
    source: str,
) -> "str | None":
    """
    Dispatch a locally-handled intent to the appropriate domain handler.

    Returns the reply string, or None if this intent is not handled locally
    (caller should fall through to queue-writing path).

    Raises nothing — all exceptions are caught and returned as error strings.
    """
    global _DISPATCH_TABLE
    if _DISPATCH_TABLE is None:
        _DISPATCH_TABLE = _build_dispatch_table()

    handler = _DISPATCH_TABLE.get(intent)
    if handler is None:
        return None  # not a local intent

    if not _is_enabled(intent):
        return f"⚠️ /{intent.replace('_', '')} is currently disabled."

    try:
        return handler(intent, message, sender, channel_id, source)
    except Exception as exc:
        import logging, traceback
        logging.getLogger("intent_registry").error(
            "handler error intent=%s: %s\n%s", intent, exc, traceback.format_exc()
        )
        return f"⚠️ Error handling /{intent}: {exc}"
