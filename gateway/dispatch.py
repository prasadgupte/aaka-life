"""gateway/dispatch.py — page a local Claude Code agent from chat.

Runs an allowlisted agent headlessly (`claude -p`) in its own directory, with a
hard guardrail system prompt, and returns a concise summary + any files it made
(via `ATTACH: <path>` markers). The reverse of the push-based agent gateway: here
aaka dispatches TO an agent on demand.

Grounding is the whole point — the agent reads its real files (verified in
testing: an agent pulled a real school-trip packing list straight from the case
file, not invented). The guardrail forbids inventing facts and destructive
actions.

Permissions (SEC-5). Headless runs used to pass `--dangerously-skip-permissions`,
which bypasses EVERY permission check — an arbitrary Bash/Write primitive driven
by a chat message. It is replaced by two hard, non-prompting limits:

  • `--permission-mode dontAsk` — nothing can escalate by asking; a tool outside
    the allowlist is simply denied instead of stalling the headless run.
  • `--allowedTools` — an explicit read-only set (Read/Glob/Grep/WebFetch/
    WebSearch/TodoWrite/NotebookRead) plus `Write(<outbox>/**)` so the agent can
    still produce the file it attaches. Bash, Edit and unscoped Write are absent,
    so they are denied.

`claude --help` on this host confirms the flag names: `--allowedTools`,
`--disallowedTools`, `--permission-mode <acceptEdits|auto|bypassPermissions|
manual|dontAsk|plan>`. There is no read-only permission mode, hence the
allowlist. Dispatch stays admin-only + dir-allowlisted regardless.

The agent registry has NO built-in entries — dispatchable dirs come only from
$AAKA_CONFIG_DIR/config/agents.yaml on the operator's own machine. An entry is
either one line (`fa: /path/to/dir`) or a dict:

    fa:
      dir: /path/to/dir
      desc: family paperwork        # shown by a bare `r`
      mode: fire                    # fire | hold — what `r fa <prompt>` does without ! or ?
      model: sonnet                 # default model; `-o` switches one run to opus
      access: read                  # only read for now
      runner: claude                # only one-shot `claude -p` for now

Usage: run_agent("fa", "the trip packing list, send the file", who="the admin")
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
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

# Allowlist: agent-id → working dir. Populated ONLY from
# $AAKA_CONFIG_DIR/config/agents.yaml ({id: {dir: ...}} or {id: dir}).
# Intentionally empty: a shipped default would hand every install a dispatchable
# directory it never opted into (SEC-5).
_DEFAULT_AGENTS: dict[str, str] = {}

# Read-only tool set for headless dispatch. Write is scoped to the outbox at
# call time (see _tool_allowlist); Bash/Edit/MultiEdit are deliberately absent.
_READONLY_TOOLS = ("Read", "Glob", "Grep", "NotebookRead", "WebFetch", "WebSearch", "TodoWrite")


def _tool_allowlist(outbox: str) -> str:
    """Comma-separated --allowedTools value: read-only tools + outbox-only Write."""
    return ",".join([*_READONLY_TOOLS, f"Write({outbox}/**)"])

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


def _outbox_dir(aid: str) -> str:
    """Private scratch dir the dispatched agent may write into.

    Not /tmp/aaka_dispatch/<id>: a world-writable, predictable path lets any
    local user pre-create or swap the files we then send to the user (SEC-11).
    Prefers $AAKA_CONFIG_DIR/data/tmp/dispatch/<id> at mode 700; falls back to a
    private mkdtemp when no config dir is configured."""
    cfg = os.environ.get("AAKA_CONFIG_DIR", "").strip()
    if cfg:
        root = Path(cfg) / "data" / "tmp" / "dispatch"
        try:
            root.mkdir(parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            d = root / re.sub(r"[^a-z0-9_-]", "_", aid)
            d.mkdir(exist_ok=True)
            os.chmod(d, 0o700)
            return str(d)
        except OSError:
            pass
    return tempfile.mkdtemp(prefix=f"aaka-dispatch-{aid}-")


_MODES = ("fire", "hold")
_RUNNERS = ("claude",)
_ACCESS = ("read",)


def _spec(raw) -> dict:
    """One agents.yaml entry → a full spec with defaults. A value this version
    can't honour falls back to the safe default (read, claude, fire)."""
    v = raw if isinstance(raw, dict) else {"dir": raw}
    mode = str(v.get("mode") or "fire").lower()
    runner = str(v.get("runner") or "claude").lower()
    access = str(v.get("access") or "read").lower()
    return {
        "dir": str(v.get("dir") or ""),
        "desc": str(v.get("desc") or ""),
        "mode": mode if mode in _MODES else "fire",
        "model": str(v.get("model") or "sonnet"),
        "runner": runner if runner in _RUNNERS else "claude",
        "access": access if access in _ACCESS else "read",
    }


def agent_specs() -> dict[str, dict]:
    """{id: spec} for every dispatchable agent (format: module docstring)."""
    out = {k: _spec(v) for k, v in _DEFAULT_AGENTS.items()}
    try:
        import yaml
        p = Path(os.environ.get("AAKA_CONFIG_DIR", "")) / "config" / "agents.yaml"
        if p.exists():
            for k, v in (yaml.safe_load(p.read_text()) or {}).items():
                out[str(k).lower()] = _spec(v)
    except Exception:
        pass
    return out


def agents() -> dict[str, str]:
    """{id: dir} — the allowlist view (SEC-5 checks dirs against this)."""
    return {k: v["dir"] for k, v in agent_specs().items()}


_FLAG = r"-[owb]"
_LATER_FLAGS = {"-w": "write access", "-b": "background runs"}


def parse_run(arg: str) -> dict:
    """Parse the text after `r` / `/run` / `/ask`.

    `<id>[!|?] [flags] <prompt> [flags]` → {agent, mode, prompt, model,
    unsupported}. `!` = fire now, `?` = hold for review, neither = the agent's
    own default (mode None). Flags count only right after the id or at the very
    end, so a `-o` in the middle of the prompt stays text. Empty → {}."""
    arg = (arg or "").strip()
    if not arg:
        return {}
    m = re.match(r"^@?([A-Za-z0-9_-]+)([!?])?(?=\s|$)", arg)
    if not m:
        return {"error": "bad_agent"}
    agent, sw = m.group(1).lower(), m.group(2)
    rest = arg[m.end():]
    flags: list[str] = []
    lead = re.match(rf"^((?:\s+{_FLAG})+)(?=\s|$)", rest)
    if lead:
        flags += lead.group(1).split()
        rest = rest[lead.end():]
    trail = re.search(rf"((?:\s+{_FLAG})+)\s*$", rest)
    if trail:
        flags += trail.group(1).split()
        rest = rest[:trail.start()]
    return {
        "agent": agent,
        "mode": {"!": "fire", "?": "hold"}.get(sw or ""),
        "prompt": rest.strip(),
        "model": "opus" if "-o" in flags else None,
        "unsupported": sorted({_LATER_FLAGS[f] for f in flags if f in _LATER_FLAGS}),
    }


def run_agent(agent_id: str, request: str, who: str = "the user",
              timeout: int = 240, model: str | None = None) -> dict:
    """Dispatch. Returns {ok, text, attachments[], error?, cost, session, duration_ms}.
    `model` None → the agent's own `model:` (default sonnet)."""
    specs = agent_specs()
    reg = {k: v["dir"] for k, v in specs.items()}
    aid = (agent_id or "").lower().lstrip("@")
    d = reg.get(aid)
    model = model or (specs.get(aid) or {}).get("model") or "sonnet"
    if not d or not Path(d).is_dir():
        return {"ok": False, "error": "unknown_agent",
                "text": f"I don't have an agent '{agent_id}'. Known: {', '.join(sorted(reg))}.",
                "attachments": []}

    outbox = _outbox_dir(aid)
    guard = _GUARDRAIL.format(who=who, outbox=outbox)
    cmd = [_claude_bin(), "-p", request, "--append-system-prompt", guard,
           "--model", model,
           "--permission-mode", "dontAsk",
           "--allowedTools", _tool_allowlist(outbox),
           "--output-format", "json"]
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
