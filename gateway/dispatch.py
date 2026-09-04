"""gateway/dispatch.py — page a local Claude Code agent from chat.

Runs an allowlisted agent headlessly (`claude -p`) in its own directory, with a
hard guardrail system prompt, and returns a concise summary + any files it made
(via `ATTACH: <path>` markers). The reverse of the push-based agent gateway: here
aaka dispatches TO an agent on demand.

Grounding is the whole point — the agent reads its real files (verified in
testing: an agent pulled a real school-trip packing list straight from the case
file, not invented). The guardrail forbids inventing facts and destructive
actions; `--dangerously-skip-permissions` is required for headless tool use, so
dispatch is admin-only + dir-allowlisted.

Usage: run_agent("fa", "the trip packing list, send the file", who="the admin")
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


def _claude_bin() -> str:
    """Resolve the claude CLI — launchd runs with a minimal PATH, so fall back to
    common install locations before giving up."""
    b = shutil.which("claude")
    if b:
        return b
    for c in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
              os.path.expanduser("~/.local/bin/claude")):
        if os.path.exists(c):
            return c
    return "claude"

# Allowlist: agent-id → working dir. Extend via $AAKA_CONFIG_DIR/config/agents.yaml
# ({id: {dir: ...}} or {id: dir}). Only dirs listed here can be dispatched.
_DEFAULT_AGENTS: dict[str, str] = {
    "fa": "/Users/Shared/family-admin",
}

_GUARDRAIL = (
    "You are invoked HEADLESSLY by aaka to answer a chat request from {who}. "
    "GUARDRAILS (hard, non-negotiable): work read-only except writing files under {outbox}; "
    "never delete, move, rename, send, git-commit, or modify any family / vault / project file; "
    "never run destructive shell. NEVER invent facts (dates, lists, numbers, names, prices) — if "
    "it is not grounded in a file or your context, say plainly what is missing and stop; do not "
    "guess. Keep the reply short: a summary a busy parent can read on a phone. If you create a "
    "file for the user, write it under {outbox} and end your reply with one line per file, exactly: "
    "ATTACH: <absolute path>"
)


def agents() -> dict[str, str]:
    out = dict(_DEFAULT_AGENTS)
    try:
        import yaml
        p = Path(os.environ.get("AAKA_CONFIG_DIR", "")) / "config" / "agents.yaml"
        if p.exists():
            for k, v in (yaml.safe_load(p.read_text()) or {}).items():
                out[k] = v.get("dir") if isinstance(v, dict) else v
    except Exception:
        pass
    return out


def run_agent(agent_id: str, request: str, who: str = "the user",
              timeout: int = 240, model: str = "sonnet") -> dict:
    """Dispatch. Returns {ok, text, attachments[], error?, cost, session, duration_ms}."""
    reg = agents()
    aid = (agent_id or "").lower().lstrip("@")
    d = reg.get(aid)
    if not d or not Path(d).is_dir():
        return {"ok": False, "error": "unknown_agent",
                "text": f"I don't have an agent '{agent_id}'. Known: {', '.join(sorted(reg))}.",
                "attachments": []}

    outbox = f"/tmp/aaka_dispatch/{aid}"
    Path(outbox).mkdir(parents=True, exist_ok=True)
    guard = _GUARDRAIL.format(who=who, outbox=outbox)
    cmd = [_claude_bin(), "-p", request, "--append-system-prompt", guard,
           "--model", model, "--dangerously-skip-permissions", "--output-format", "json"]
    try:
        proc = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "attachments": [],
                "text": f"{agent_id} took longer than {timeout}s and was stopped."}
    except Exception as e:
        return {"ok": False, "error": "run_error", "attachments": [], "text": str(e)[:300]}

    try:
        data = json.loads((proc.stdout or "").strip() or "{}")
        result = data.get("result", "") or ""
    except Exception:
        return {"ok": False, "error": "bad_output", "attachments": [],
                "text": (proc.stdout or proc.stderr or "").strip()[:500]}

    # Split ATTACH: markers from the human-facing text.
    attachments, lines = [], []
    for ln in result.splitlines():
        m = re.match(r"\s*ATTACH:\s*(.+?)\s*$", ln)
        if m:
            p = m.group(1).strip().strip('"').strip("'")
            if Path(p).is_file():
                attachments.append(p)
        else:
            lines.append(ln)
    return {"ok": True, "text": "\n".join(lines).strip(), "attachments": attachments,
            "cost": data.get("total_cost_usd"), "session": data.get("session_id"),
            "duration_ms": data.get("duration_ms")}
