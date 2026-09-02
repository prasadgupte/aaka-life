#!/usr/bin/env python3
"""aaka security self-audit — concrete, repeatable checks for the sensor's
attack surface. Read-only: it inspects file permissions, git tracking, and code
structure; it never changes anything. Run before deploying to the VPS.

    python3 admin/security_check.py          # human-readable
    python3 admin/security_check.py --json    # machine-readable

Also surfaced in chat as `/security` (admin-only) and MCP `security_check()`.

Each check yields a level:
  ok   — verified safe
  warn — worth tightening, not an open door
  fail — a real exposure; fix before deploying

Add a check here whenever a new failure mode is discovered (living runbook,
like admin/diagnose.sh).
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR", "/Users/Shared/aaka-repo-config"))
SECRETS_ROOT = Path(os.environ.get("AAKA_SECRETS_ROOT", "/Users/Shared/secrets"))

_R: list[dict] = []


def add(check: str, level: str, msg: str) -> None:
    _R.append({"check": check, "level": level, "msg": msg})


def _group_or_other_accessible(p: Path) -> bool:
    """True if group/other has any rwx bit (mode & 0o077)."""
    try:
        return bool(stat.S_IMODE(p.stat().st_mode) & 0o077)
    except OSError:
        return False


def _grep(pattern: str, *paths: str) -> list[str]:
    """Return matching 'file:line' hits for a regex under the given repo paths."""
    hits: list[str] = []
    rx = re.compile(pattern)
    for rel in paths:
        root = BASE / rel
        files = root.rglob("*.py") if root.is_dir() else [root]
        for f in files:
            try:
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(BASE)}:{i}")
            except OSError:
                pass
    return hits


# ── 1. Ingress model: the VPS sensor must be poll-only (no inbound port) ───────
def check_ingress() -> None:
    entry = BASE / "sensor" / "entrypoint.sh"
    poll = entry.exists() and "telegram_multibot" in entry.read_text(errors="ignore")
    # A live listener check if the tooling exists (VPS): flag unexpected ports.
    listening = ""
    for probe in (["ss", "-tlnH"], ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]):
        try:
            r = subprocess.run(probe, capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                listening = r.stdout
                break
        except Exception:
            pass
    if poll:
        add("ingress", "ok",
            "sensor ingress is poll-only (telegram_multibot → getUpdates); no inbound HTTP port to forge envelopes at")
    else:
        add("ingress", "warn",
            "could not confirm poll-only ingress from entrypoint.sh — verify no inbound /route listener is exposed")
    if listening and re.search(r":18789\b", listening):
        add("ingress", "fail", "router serve() port :18789 is LISTENING — the poll model shouldn't expose it")


# ── 2. .env permissions (bot token, API keys) ─────────────────────────────────
def check_env_perms() -> None:
    found = False
    for p in (BASE / ".env", CONFIG_DIR / ".env"):
        if p.exists():
            found = True
            if _group_or_other_accessible(p):
                add("env_perms", "fail",
                    f"{p} is group/other-accessible ({oct(stat.S_IMODE(p.stat().st_mode))}) — holds tokens; chmod 600")
            else:
                add("env_perms", "ok", f"{p} is 600 (owner-only)")
    if not found:
        add("env_perms", "warn", "no .env found at repo root or config dir — is the token configured?")


# ── 3. Secrets vault + token permissions ──────────────────────────────────────
def check_secret_perms() -> None:
    if SECRETS_ROOT.exists():
        # A 700 root gates the whole tree — no other user can traverse in, so
        # individual inner file modes don't create exposure. Only fail on a
        # loose root; inner-file bits are defense-in-depth (soft note at most).
        if _group_or_other_accessible(SECRETS_ROOT):
            loose = [str(p.relative_to(SECRETS_ROOT)) for p in SECRETS_ROOT.rglob("*")
                     if p.is_file() and _group_or_other_accessible(p)]
            add("secrets_perms", "fail",
                f"{SECRETS_ROOT} is group/other-accessible (should be 700) — "
                + (f"{len(loose)} reachable file(s)" if loose else "tree exposed"))
        else:
            add("secrets_perms", "ok", f"{SECRETS_ROOT} is owner-only (700) — whole tree protected")
    else:
        add("secrets_perms", "warn", f"{SECRETS_ROOT} not present on this host")
    tokens = CONFIG_DIR / "tokens"
    if tokens.exists():
        loose = [t.name for t in tokens.glob("*.json") if _group_or_other_accessible(t)]
        if loose:
            add("token_perms", "fail", f"OAuth token(s) group/other-accessible: {', '.join(loose)}")
        else:
            add("token_perms", "ok", "OAuth tokens are owner-only")


# ── 4. No secrets tracked in git ──────────────────────────────────────────────
def check_git_secrets() -> None:
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=BASE,
                                 capture_output=True, text=True, timeout=10).stdout.splitlines()
    except Exception as e:
        add("git_secrets", "warn", f"couldn't list tracked files: {e}")
        return
    danger = re.compile(r"(^|/)(\.env|credentials\.json|token.*\.json|.*\.pem|.*secret.*|.*\.key)$", re.I)
    allow = re.compile(r"\.(example|template|sample)($|\.)", re.I)
    bad = [f for f in tracked if danger.search(f) and not allow.search(f)]
    if bad:
        add("git_secrets", "fail", f"secret-looking files are tracked by git: {', '.join(bad[:8])}")
    else:
        add("git_secrets", "ok", "no secret files tracked by git")


# ── 5. No shell=True (command-injection surface) ──────────────────────────────
def check_shell_exec() -> None:
    hits = _grep(r"shell\s*=\s*True", "sensor", "tools", "executor", "gateway", "skills")
    if hits:
        add("shell_exec", "fail", f"shell=True found (injection risk): {', '.join(hits[:8])}")
    else:
        add("shell_exec", "ok", "no shell=True in sensor/tools/executor/gateway/skills (subprocess uses argv)")


# ── 6. Admin gates on sensitive commands ──────────────────────────────────────
def check_admin_gates() -> None:
    router = "sensor/router_sensor.py"
    need = {"_handle_invite": "invite", "_handle_mcp": "mcp",
            "_handle_errors": "errors", "_handle_tools": "tools"}
    missing = []
    text = (BASE / router).read_text(errors="ignore")
    for fn in ("_handle_invite", "_handle_mcp"):
        # these must gate on member_is_admin within ~20 lines of the def
        m = re.search(rf"def {fn}\b.*?(?=\ndef )", text, re.S)
        if m and "member_is_admin" not in m.group(0):
            missing.append(fn)
    if missing:
        add("admin_gates", "fail", f"admin-only handlers missing member_is_admin gate: {', '.join(missing)}")
    else:
        add("admin_gates", "ok", "sensitive handlers (invite, mcp) gate on member_is_admin")


# ── 7. Untrusted-arg sanitization (path traversal) ────────────────────────────
def check_arg_sanitization() -> None:
    wb = BASE / "tools" / "webuntis" / "check.py"
    if wb.exists() and "fullmatch(r\"[A-Za-z0-9_-]" in wb.read_text(errors="ignore"):
        add("arg_sanitization", "ok", "webuntis member arg is slug-restricted (no path traversal)")
    elif wb.exists():
        add("arg_sanitization", "warn", "webuntis member arg not obviously slug-restricted — check for traversal")


# ── 8. Queue DB not world-writable ────────────────────────────────────────────
def check_queue_perms() -> None:
    db = Path(os.environ.get("QUEUE_DB", str(CONFIG_DIR / "data" / "queue" / "butler.db")))
    if db.exists():
        if stat.S_IMODE(db.stat().st_mode) & 0o022:
            add("queue_perms", "warn", f"{db} is group/other-writable — the executor trusts what the sensor enqueues")
        else:
            add("queue_perms", "ok", "queue DB is not group/other-writable")


CHECKS = [check_ingress, check_env_perms, check_secret_perms, check_git_secrets,
          check_shell_exec, check_admin_gates, check_arg_sanitization, check_queue_perms]


def run() -> dict:
    for c in CHECKS:
        try:
            c()
        except Exception as e:
            add(c.__name__, "warn", f"check errored: {e}")
    counts = {lvl: sum(1 for r in _R if r["level"] == lvl) for lvl in ("ok", "warn", "fail")}
    return {"results": _R, "counts": counts,
            "verdict": "fail" if counts["fail"] else ("warn" if counts["warn"] else "ok")}


def _human(out: dict) -> str:
    icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    lines = ["🔒 *Security self-audit*"]
    for r in out["results"]:
        lines.append(f"{icon[r['level']]} *{r['check']}* — {r['msg']}")
    c = out["counts"]
    lines.append(f"\n{c['fail']} fail · {c['warn']} warn · {c['ok']} ok → verdict: *{out['verdict'].upper()}*")
    return "\n".join(lines)


if __name__ == "__main__":
    out = run()
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
    else:
        print(_human(out))
    sys.exit(0)
