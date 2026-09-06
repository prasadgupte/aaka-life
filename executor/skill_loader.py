"""
Aaka — Skill Loader

Single dispatch gate for all executor skill calls.
Reads skills/registry.yaml on every call (hot-reload).
Enforces: enabled, runs_on, confirmation gate, unknown-user approval.
Appends audit log to $LOGS_DIR/skills/skill-audit.log.

Exceptions:
  SkillNotFound       — no registry entry for intent
  SkillDisabled       — skill.enabled == false
  SkillNotAllowed     — skill doesn't run on this component
  UnknownUser         — sender not in aaka_config members
  AwaitingConfirmation — requires_confirmation and not yet approved
"""

import hashlib
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))

import aaka_config


# ── Custom exceptions ──────────────────────────────────────────────────────────

class SkillNotFound(Exception):
    pass

class SkillDisabled(Exception):
    pass

class SkillDependencyDisabled(Exception):
    pass

class SkillNotAllowed(Exception):
    pass

class UnknownUser(Exception):
    pass

class AwaitingConfirmation(Exception):
    pass


# ── SkillLoader ────────────────────────────────────────────────────────────────

class SkillLoader:
    def __init__(self, registry_path: str | Path, logs_dir: str | Path):
        self.registry_path = Path(registry_path)
        self.logs_dir = Path(logs_dir)
        self._audit_log = self.logs_dir / "skills" / "skill-audit.log"

    def _load_registry(self) -> dict:
        with open(self.registry_path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("skills", {})

    def resolve(self, intent: str, sender: str = "") -> dict:
        """Find skill entry by intent. Raises SkillNotFound if no match.

        If sender is provided and not in aaka_config members, queues owner
        approval and raises UnknownUser.
        """
        if sender:
            import aaka_config
            member = aaka_config.member_by_sender(sender)
            if member is None:
                self._queue_owner_approval(intent, sender)
                raise UnknownUser(f"Unknown sender: {sender}")

        registry = self._load_registry()
        for key, entry in registry.items():
            if entry.get("intent") == intent:
                return {"_key": key, **entry}
        raise SkillNotFound(f"No skill registered for intent: {intent!r}")

    def validate(self, skill: dict, context: dict) -> None:
        """Check enabled, runs_on, and dependency constraints.

        context: {"runs_on": "home"} (or "away", or the legacy names
        "executor"/"sensor"). The canonical labels are `away` (stateless
        sensor on a VPS) and `home` (executor on the machine that holds
        OAuth tokens); legacy labels are accepted on both sides.
        Raises SkillDisabled, SkillNotAllowed, or SkillDependencyDisabled.
        """
        if not skill.get("enabled", True):
            raise SkillDisabled(f"Skill {skill.get('_key', '?')} is disabled")

        _ROLE_EQ = {"sensor": "away", "executor": "home", "away": "away", "home": "home"}
        runs_on = [_ROLE_EQ.get(r, r) for r in skill.get("runs_on", [])]
        component_raw = context.get("runs_on", "")
        component = _ROLE_EQ.get(component_raw, component_raw)
        if component and runs_on and component not in runs_on:
            raise SkillNotAllowed(
                f"Skill {skill.get('_key', '?')} does not run on {component!r} "
                f"(runs_on={runs_on})"
            )

        self._validate_dependencies(skill)

    def _validate_dependencies(self, skill: dict) -> None:
        """Check that all skills listed in depends_on are enabled.

        Raises SkillDependencyDisabled if a dependency is disabled.
        """
        deps = skill.get("depends_on") or []
        if not deps:
            return
        registry = self._load_registry()
        for dep_key in deps:
            dep = registry.get(dep_key)
            if dep is None:
                continue  # unknown dep key — don't block, just skip
            if not dep.get("enabled", True):
                raise SkillDependencyDisabled(
                    f"Skill {skill.get('_key', '?')} requires '{dep_key}' which is disabled"
                )

    def check_confirmation(self, skill: dict, item_id: str) -> None:
        """If skill requires_confirmation, verify approved token exists.

        Raises AwaitingConfirmation if pending.
        """
        if not skill.get("requires_confirmation", False):
            return

        from aaka_queue.queue import pending_confirms
        confirmed = pending_confirms(item_id)
        if not confirmed:
            raise AwaitingConfirmation(
                f"Skill {skill.get('_key', '?')} awaiting confirmation for item {item_id}"
            )

    def execute(self, skill: dict, payload: dict, dispatcher_fn=None) -> dict:
        """Dynamically call the skill entrypoint's execute() or main() function.

        Falls back to dispatcher_fn if entrypoint is null/absent (legacy path).
        Appends audit log entry regardless of success/failure.
        """
        key = skill.get("_key", "unknown")
        intent = skill.get("intent", "unknown")
        entrypoint = skill.get("entrypoint")

        try:
            if entrypoint and dispatcher_fn is None:
                result = self._import_and_call(entrypoint, payload)
            elif dispatcher_fn is not None:
                result = dispatcher_fn(payload)
            else:
                raise SkillNotFound(f"Skill {key} has no entrypoint and no fallback dispatcher")

            result_hash = self._hash(result)
            self._audit(key, intent, "ok", result_hash)
            return result

        except Exception as exc:
            self._audit(key, intent, "error", str(exc)[:80])
            raise

    def _import_and_call(self, entrypoint: str, payload: dict) -> dict:
        ep_path = BASE / entrypoint
        spec = importlib.util.spec_from_file_location("_skill_ep", ep_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "execute", None) or getattr(mod, "main", None)
        if fn is None:
            raise AttributeError(f"Entrypoint {entrypoint} has no execute() or main()")
        return fn(payload)

    def _audit(self, skill_key: str, intent: str, status: str, result_hash: str) -> None:
        self._audit_log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"{ts} | {intent} | {skill_key} | {status} | {result_hash}\n"
        with open(self._audit_log, "a") as f:
            f.write(line)

    def _hash(self, result: dict) -> str:
        import json
        raw = json.dumps(result, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def _is_blocked_sender(self, sender: str) -> bool:
        """Check if sender is in the blocked_senders.json list."""
        import json as _json
        config_dir = Path(os.environ.get("AAKA_CONFIG_DIR") or aaka_config.CONFIG_DIR)
        path = config_dir / "data" / "blocked_senders.json"
        if not path.exists():
            return False
        try:
            return bool(_json.loads(path.read_text()).get(sender))
        except Exception:
            return False

    def _queue_owner_approval(self, intent: str, sender: str) -> None:
        """Write an outbox message to the owner asking to approve this unknown sender.

        Silently skips if the sender has been previously blocked via /deny.
        """
        if self._is_blocked_sender(sender):
            return  # silently ignore — owner already denied this sender

        try:
            import aaka_config
            from aaka_queue.queue import write_outbox

            # Find the first member with role=owner or just first member
            owner = None
            for m in aaka_config.members():
                if m.get("role") == "owner" or owner is None:
                    owner = m
            if owner is None:
                return

            channel = str(owner.get("telegram") or owner.get("whatsapp") or "")
            if not channel:
                return

            text = (
                f"Unknown sender requested '{intent}': {sender}\n"
                f"Reply /approve {sender} to allow, or /deny {sender} to block."
            )
            write_outbox(
                channel_id=channel,
                sender=channel,
                text=text,
                source="telegram",
            )
        except Exception:
            pass  # non-fatal: audit log already records the UnknownUser event
