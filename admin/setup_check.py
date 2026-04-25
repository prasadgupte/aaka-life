#!/usr/bin/env python3
"""
admin/setup_check.py — Tier-aware setup checker for Aaka.

Designed for LLM-guided onboarding (CLAUDE_SETUP.md). Produces clean, structured
output that Claude Code can parse and act on. Each check is independent; partial
setups report which tiers are reachable and which step unlocks the next tier.

Usage:
  python3 admin/setup_check.py          # human-readable summary
  python3 admin/setup_check.py --json   # machine-readable JSON for LLM parsing
  python3 admin/setup_check.py --tier 1 # show checks for a specific tier only
"""

import json
import os
import sys
import subprocess
from pathlib import Path

# ── Resolve paths (no aaka_config import — this runs before config exists) ────

REPO_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR") or Path.home() / ".aaka")

# ── Check helpers ─────────────────────────────────────────────────────────────

def _exists(path: Path) -> bool:
    return path.exists()

def _env_from_dotenv() -> dict:
    env_file = REPO_DIR / ".env"
    result = {}
    if not env_file.exists():
        return result
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result

def _yaml_exists_and_parseable(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "file not found"
    try:
        import yaml
        yaml.safe_load(path.read_text())
        return True, "ok"
    except Exception as e:
        return False, str(e)

def _docker_running() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def _container_up(name: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5
        )
        return name in r.stdout.splitlines()
    except Exception:
        return False

def _token_valid(token_file: Path) -> tuple[bool, str]:
    if not token_file.exists():
        return False, "token not found"
    try:
        data = json.loads(token_file.read_text())
        if "token" in data or "access_token" in data:
            return True, "present"
        return False, "unexpected format"
    except Exception as e:
        return False, str(e)

def _venv_ok() -> bool:
    return (REPO_DIR / "venv" / "bin" / "python3").exists()

def _python_import(module: str) -> bool:
    py = str(REPO_DIR / "venv" / "bin" / "python3")
    if not Path(py).exists():
        py = sys.executable
    r = subprocess.run(
        [py, "-c", f"import {module}"],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPO_DIR)},
    )
    return r.returncode == 0

# ── Individual checks ─────────────────────────────────────────────────────────

def check_config_dir() -> dict:
    ok = CONFIG_DIR.exists()
    return {
        "id": "config_dir",
        "tier": 0,
        "label": "Config directory",
        "ok": ok,
        "value": str(CONFIG_DIR),
        "fix": f'mkdir -p "{CONFIG_DIR}/config" "{CONFIG_DIR}/tokens" "{CONFIG_DIR}/data/queue" "{CONFIG_DIR}/data/calendar" "{CONFIG_DIR}/logs" "{CONFIG_DIR}/openclaw-data"',
        "note": "" if ok else f"Run the fix command, then set AAKA_CONFIG_DIR={CONFIG_DIR} in your shell",
    }

def check_dotenv() -> dict:
    env_file = REPO_DIR / ".env"
    ok = env_file.exists()
    env = _env_from_dotenv()
    has_token = bool(env.get("TELEGRAM_BOT_TOKEN"))
    has_uid = bool(env.get("TELEGRAM_USER_ID"))
    detail = []
    if not ok:
        detail.append("missing")
    else:
        if not has_token:
            detail.append("TELEGRAM_BOT_TOKEN missing")
        if not has_uid:
            detail.append("TELEGRAM_USER_ID missing")
    return {
        "id": "dotenv",
        "tier": 0,
        "label": ".env file",
        "ok": ok and has_token and has_uid,
        "value": str(env_file) if ok else "not found",
        "fix": f'Create {REPO_DIR}/.env with TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID',
        "note": ", ".join(detail) or "ok",
        "has_telegram_token": has_token,
        "has_telegram_uid": has_uid,
        "has_gemini_key": bool(env.get("GEMINI_API_KEY")),
    }

def check_aaka_yaml() -> dict:
    yaml_path = CONFIG_DIR / "config" / "aaka.yaml"
    ok, msg = _yaml_exists_and_parseable(yaml_path)
    members = []
    if ok:
        try:
            import yaml
            data = yaml.safe_load(yaml_path.read_text()) or {}
            members = [m.get("id", "?") for m in data.get("members", [])]
        except Exception:
            pass
    return {
        "id": "aaka_yaml",
        "tier": 0,
        "label": "aaka.yaml",
        "ok": ok,
        "value": str(yaml_path),
        "fix": f'Copy config/sample/aaka.yaml.example to {yaml_path} and fill in your timezone and Telegram ID',
        "note": msg if not ok else f"{len(members)} member(s): {', '.join(members)}",
        "member_count": len(members),
    }

def check_venv() -> dict:
    ok = _venv_ok()
    return {
        "id": "venv",
        "tier": 0,
        "label": "Python venv",
        "ok": ok,
        "value": str(REPO_DIR / "venv") if ok else "not found",
        "fix": f'cd {REPO_DIR} && python3 -m venv venv && venv/bin/pip install -r requirements.txt -q',
        "note": "" if ok else "Run the fix command",
    }

def check_docker() -> dict:
    ok = _docker_running()
    return {
        "id": "docker",
        "tier": 0,
        "label": "Docker",
        "ok": ok,
        "value": "running" if ok else "not running",
        "fix": "Install Docker from docker.com/get-started and start Docker Desktop",
        "note": "" if ok else "Docker is required to run the Telegram sensor",
    }

def check_sensor() -> dict:
    up = _container_up("aaka-sensor-dev")
    return {
        "id": "sensor",
        "tier": 0,
        "label": "Sensor container",
        "ok": up,
        "value": "aaka-sensor-dev running" if up else "not running",
        "fix": f'cd {REPO_DIR} && AAKA_CONFIG_DIR_HOST="{CONFIG_DIR}" docker compose up -d sensor',
        "note": "" if up else "Start with the fix command; check logs with: docker logs aaka-sensor-dev --tail 20",
    }

def check_executor() -> dict:
    log = CONFIG_DIR / "logs" / "queueworker.log"
    ok = log.exists()
    return {
        "id": "executor",
        "tier": 0,
        "label": "Executor (queue worker)",
        "ok": ok,
        "value": str(log) if ok else "log not found (never run?)",
        "fix": f'AAKA_CONFIG_DIR="{CONFIG_DIR}" {REPO_DIR}/venv/bin/python3 {REPO_DIR}/executor/queue_worker.py &',
        "note": "" if ok else "Run once with --once to verify: executor/queue_worker.py --once",
    }

def check_google_credentials() -> dict:
    user_creds = CONFIG_DIR / "tokens" / "credentials.json"
    bundled = REPO_DIR / "config" / "credentials.json"
    if user_creds.exists():
        src = "user-provided"
        ok = True
        path = user_creds
    elif bundled.exists():
        src = "bundled community app"
        ok = True
        path = bundled
    else:
        src = "none"
        ok = False
        path = None
    return {
        "id": "google_credentials",
        "tier": 1,
        "label": "Google OAuth credentials",
        "ok": ok,
        "value": f"{src}: {path}" if path else "not found",
        "fix": f'bundled credentials.json is included — run: AAKA_CONFIG_DIR="{CONFIG_DIR}" venv/bin/python3 admin/reauth.py',
        "note": "" if ok else "credentials.json missing — should be bundled with the repo",
    }

def check_google_token() -> dict:
    token_path = CONFIG_DIR / "tokens" / "token.json"
    ok, msg = _token_valid(token_path)
    return {
        "id": "google_token",
        "tier": 1,
        "label": "Google OAuth token",
        "ok": ok,
        "value": str(token_path) if token_path.exists() else "not found",
        "fix": f'AAKA_CONFIG_DIR="{CONFIG_DIR}" venv/bin/python3 admin/reauth.py',
        "note": msg,
    }

def check_calendar_access() -> dict:
    py = str(REPO_DIR / "venv" / "bin" / "python3")
    if not Path(py).exists():
        return {
            "id": "calendar_access",
            "tier": 1,
            "label": "Google Calendar access",
            "ok": False,
            "value": "skipped — venv not set up",
            "fix": "Set up venv first (Tier 0)",
            "note": "requires venv",
        }
    r = subprocess.run(
        [py, "-c",
         "import sys; sys.path.insert(0, ''); "
         "from skills.calendar import gog; cals = gog.list_calendars(); "
         f"print(len(cals))"],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "AAKA_CONFIG_DIR": str(CONFIG_DIR), "AAKA_BASE": str(REPO_DIR)},
        cwd=str(REPO_DIR),
    )
    if r.returncode == 0:
        count = r.stdout.strip()
        return {
            "id": "calendar_access",
            "tier": 1,
            "label": "Google Calendar access",
            "ok": True,
            "value": f"{count} calendar(s)",
            "fix": "",
            "note": "ok",
        }
    return {
        "id": "calendar_access",
        "tier": 1,
        "label": "Google Calendar access",
        "ok": False,
        "value": "failed",
        "fix": f'AAKA_CONFIG_DIR="{CONFIG_DIR}" venv/bin/python3 admin/reauth.py',
        "note": (r.stderr or r.stdout or "unknown error").strip()[:200],
    }

def check_gemini_key() -> dict:
    env = _env_from_dotenv()
    key = env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    ok = bool(key)
    return {
        "id": "gemini_key",
        "tier": 2,
        "label": "Gemini API key",
        "ok": ok,
        "value": "set" if ok else "missing",
        "fix": "Get a free key at aistudio.google.com → Get API key. Add GEMINI_API_KEY=... to .env",
        "note": "" if ok else "Without this, natural language parsing is disabled. Tasks/lists/notes still work.",
    }

def check_vps() -> dict:
    env = _env_from_dotenv()
    vps_ip = env.get("AAKA_VPS_IP") or os.environ.get("AAKA_VPS_IP", "")
    ok = bool(vps_ip)
    return {
        "id": "vps",
        "tier": 3,
        "label": "VPS configured",
        "ok": ok,
        "value": vps_ip if ok else "not configured",
        "fix": "See INSTALL.md Phase 3 — VPS is optional. Bot runs locally without it.",
        "note": "" if ok else "VPS enables always-on (bot responds when Mac is asleep) and WhatsApp support.",
    }

# ── Tier analysis ─────────────────────────────────────────────────────────────

TIER_REQUIREMENTS = {
    0: ["config_dir", "dotenv", "aaka_yaml", "venv", "docker", "sensor", "executor"],
    1: ["google_credentials", "google_token", "calendar_access"],
    2: ["gemini_key"],
    3: ["vps"],
}

TIER_LABELS = {
    0: "Tier 0 — Telegram (tasks, lists, notes)",
    1: "Tier 1 — Google Calendar",
    2: "Tier 2 — Natural language",
    3: "Tier 3 — Always-on VPS",
}

# ── Main ──────────────────────────────────────────────────────────────────────

def run_checks(tier_filter: int | None = None) -> list[dict]:
    all_checks = [
        check_config_dir(),
        check_dotenv(),
        check_aaka_yaml(),
        check_venv(),
        check_docker(),
        check_sensor(),
        check_executor(),
        check_google_credentials(),
        check_google_token(),
        check_calendar_access(),
        check_gemini_key(),
        check_vps(),
    ]
    if tier_filter is not None:
        return [c for c in all_checks if c["tier"] == tier_filter]
    return all_checks


def tier_status(checks: list[dict]) -> dict[int, dict]:
    by_tier: dict[int, list[dict]] = {}
    for c in checks:
        by_tier.setdefault(c["tier"], []).append(c)

    status = {}
    for tier, req_ids in TIER_REQUIREMENTS.items():
        tier_checks = {c["id"]: c for c in by_tier.get(tier, [])}
        failing = [cid for cid in req_ids if not tier_checks.get(cid, {}).get("ok")]
        status[tier] = {
            "reachable": len(failing) == 0,
            "failing": failing,
            "first_fix": tier_checks.get(failing[0], {}).get("fix", "") if failing else "",
        }
    return status


def print_human(checks: list[dict]) -> None:
    GREEN = "\033[0;32m"
    RED = "\033[0;31m"
    YELLOW = "\033[1;33m"
    BOLD = "\033[1m"
    NC = "\033[0m"

    print(f"\n{BOLD}Aaka Setup Status{NC}")
    print(f"Config: {CONFIG_DIR}")
    print(f"Repo:   {REPO_DIR}")
    print()

    tier_s = tier_status(checks)
    checks_by_id = {c["id"]: c for c in checks}

    for tier in sorted(TIER_REQUIREMENTS.keys()):
        ts = tier_s[tier]
        label = TIER_LABELS[tier]
        status_mark = f"{GREEN}✓{NC}" if ts["reachable"] else f"{RED}✗{NC}"
        print(f"  {status_mark}  {BOLD}{label}{NC}")

        for cid in TIER_REQUIREMENTS[tier]:
            c = checks_by_id.get(cid, {})
            ok = c.get("ok", False)
            mark = f"{GREEN}✓{NC}" if ok else f"{RED}✗{NC}"
            note = c.get("note", "")
            val = c.get("value", "")
            suffix = f" — {note}" if note and note != "ok" else (f" ({val})" if val and val != "ok" else "")
            print(f"       {mark}  {c.get('label', cid)}{suffix}")

        if not ts["reachable"] and ts["failing"]:
            first_failing = checks_by_id.get(ts["failing"][0], {})
            print(f"\n       {YELLOW}→ Next step:{NC} {first_failing.get('label', ts['failing'][0])}")
            fix = first_failing.get("fix", "")
            if fix:
                print(f"         {fix}")
        print()

    # First blocked tier summary
    for tier in sorted(TIER_REQUIREMENTS.keys()):
        if not tier_s[tier]["reachable"]:
            print(f"{BOLD}To unlock {TIER_LABELS[tier]}:{NC}")
            for cid in tier_s[tier]["failing"]:
                c = checks_by_id.get(cid, {})
                fix = c.get("fix", "")
                if fix:
                    print(f"  {c.get('label', cid)}:")
                    print(f"    {fix}")
            break


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Aaka setup checker")
    parser.add_argument("--json", action="store_true", help="Output JSON for LLM parsing")
    parser.add_argument("--tier", type=int, help="Show checks for specific tier only")
    args = parser.parse_args()

    checks = run_checks(args.tier)

    if args.json:
        ts = tier_status(checks)
        output = {
            "config_dir": str(CONFIG_DIR),
            "repo_dir": str(REPO_DIR),
            "tiers": {
                str(t): {
                    "label": TIER_LABELS[t],
                    "reachable": ts[t]["reachable"],
                    "failing": ts[t]["failing"],
                    "next_fix": ts[t]["first_fix"],
                }
                for t in sorted(TIER_REQUIREMENTS.keys())
            },
            "checks": checks,
        }
        print(json.dumps(output, indent=2))
    else:
        print_human(checks)


if __name__ == "__main__":
    main()
