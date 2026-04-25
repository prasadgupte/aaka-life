#!/usr/bin/env bash
# admin/skills.sh — Skill inventory: descriptions, permissions, deps, stats
#
# Run: bash admin/aaka.sh skills
#      bash admin/skills.sh
#
# Works on both &Home (local) and &Away (VPS).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/detect.sh"

if [ "$INSTANCE" = "local" ] && [ -f "$REPO_DIR/venv/bin/python3" ]; then
    PYTHON="$REPO_DIR/venv/bin/python3"
else
    PYTHON="python3"
fi

REGISTRY="$REPO_DIR/skills/registry.yaml"
QUEUE_DB="${QUEUE_DB:-$AAKA_CONFIG_DIR/data/queue/butler.db}"
CALENDAR_DIR="$AAKA_CONFIG_DIR/data/calendar"
LOGS_DIR="$AAKA_CONFIG_DIR/logs"

echo -e "${BOLD}Aaka Skill Inventory${NC}"
echo "===================="
info "Instance:  $INSTANCE ($MODE)"
info "Repo:      $REPO_DIR"
info "ConfigDir: $AAKA_CONFIG_DIR"

# ── Member roster (for permissions display) ──────────────────────────────────
header "Members & Auth"

$PYTHON - <<'PYEOF'
import sys, os
sys.path.insert(0, os.environ.get("AAKA_BASE", ""))
os.environ.setdefault("AAKA_CONFIG_DIR", os.path.expanduser("~/.aaka"))
try:
    import aaka_config
    members = aaka_config.members()
    print(f"  Members registered: {len(members)}")
    for m in members:
        roles = []
        if m.get("auth"):
            roles.append("auth:" + ",".join(m["auth"].get("scopes", [])))
        if m.get("is_carrier"):
            roles.append("carrier")
        if m.get("invite_as_guest"):
            roles.append("guest-invites")
        channels = []
        if m.get("telegram"):
            channels.append(f"telegram:{m['telegram']}")
        if m.get("whatsapp"):
            channels.append(f"whatsapp:{m['whatsapp']}")
        role_str = f"  [{', '.join(roles)}]" if roles else ""
        ch_str = "  " + ", ".join(channels) if channels else "  (no channels)"
        print(f"    • {m['name']}{role_str}")
        print(f"      {ch_str}")
except Exception as e:
    print(f"  WARN: could not load members — {e}")
PYEOF

# ── Skill registry ───────────────────────────────────────────────────────────
header "Skills (from registry.yaml)"

if [ ! -f "$REGISTRY" ]; then
    fail "registry.yaml not found at $REGISTRY"
    exit 1
fi

$PYTHON - <<PYEOF
import sys, os, yaml
from pathlib import Path

REPO = os.environ.get("AAKA_BASE", "")
CONFIG_DIR = os.environ.get("AAKA_CONFIG_DIR") or os.path.expanduser("~/.aaka")
REGISTRY = "$REGISTRY"
QUEUE_DB = "$QUEUE_DB"
CALENDAR_DIR = "$CALENDAR_DIR"
FLIGHTS_DIR = "$FLIGHTS_DIR"
LOGS_DIR = "$LOGS_DIR"
INSTANCE = "$INSTANCE"

GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
NC     = "\033[0m"

def ok(s):   print(f"  {GREEN}✓{NC}  {s}")
def fail(s): print(f"  {RED}✗{NC}  {s}")
def warn(s): print(f"  {YELLOW}!{NC}  {s}")
def info(s): print(f"  {CYAN}▸{NC}  {s}")

with open(REGISTRY) as f:
    reg = yaml.safe_load(f)

skills = reg.get("skills", {})

# ── Skill metadata table ─────────────────────────────────────────────────────
SKILL_META = {
    "calendar_add_event": {
        "desc": "Natural language → Google Calendar event (LLM extraction, standard title format)",
        "deps": ["google_auth", "gog", "calendar_sync"],
        "trigger": "/add, 'add event', 'add appointment'",
        "tokens": "LLM",
    },
    "calendar_sync": {
        "desc": "GCal → today.md / weekly.md daemon (runs as launchd on Mac)",
        "deps": ["google_auth"],
        "trigger": "background / launchd",
        "tokens": "0",
    },
    "task_manager": {
        "desc": "CRUD tasks in butler.db; syncs Tasks.md; optional calendar bridge",
        "deps": ["queue_db", "tasks_table"],
        "trigger": "/addtask, /task, 'remind me to'",
        "tokens": "LLM",
    },
    "task_add": {
        "desc": "Legacy task module — superseded by task_manager",
        "deps": [],
        "trigger": "N/A",
        "tokens": "N/A",
    },
    "mail": {
        "desc": "Gmail integration — stub, not yet enabled",
        "deps": ["google_auth", "gmail_scope"],
        "trigger": "N/A",
        "tokens": "N/A",
    },
    "inbox_router": {
        "desc": "Zero-LLM tag resolution; routes vault inbox files to destinations",
        "deps": ["vault"],
        "trigger": "/route <tag> <content>",
        "tokens": "0 (fallback LLM)",
    },
    "build_indexes": {
        "desc": "Rebuilds vault search indexes (System/.indexes)",
        "deps": ["vault"],
        "trigger": "background",
        "tokens": "0",
    },
    "queue_test": {
        "desc": "Minimal echo handler — queue smoke test",
        "deps": ["queue_db"],
        "trigger": "/tqueue",
        "tokens": "0",
    },
    "executor_echo": {
        "desc": "End-to-end echo test (&Away → queue → &Home → outbox → &Away)",
        "deps": ["queue_db", "outbox"],
        "trigger": "/texec",
        "tokens": "0",
    },
}

# ── Dependency checkers ──────────────────────────────────────────────────────
def check_dep(dep):
    """Return (ok: bool, detail: str)"""
    if dep == "google_auth":
        cred = Path(CONFIG_DIR) / "tokens" / "credentials.json"
        tok  = Path(CONFIG_DIR) / "tokens" / "token.json"
        if cred.exists() and tok.exists():
            return True, "credentials.json + token.json present"
        missing = []
        if not cred.exists(): missing.append("credentials.json")
        if not tok.exists():  missing.append("token.json")
        return False, f"MISSING: {', '.join(missing)}"
    elif dep == "gog":
        p = Path(REPO) / "skills" / "calendar" / "gog.py"
        return p.exists(), str(p)
    elif dep == "calendar_sync":
        today = Path(CALENDAR_DIR) / "today.md"
        weekly = Path(CALENDAR_DIR) / "weekly.md"
        if today.exists() and weekly.exists():
            import time
            age = int(time.time() - today.stat().st_mtime)
            h, m = divmod(age // 60, 60)
            return True, f"today.md age: {h}h{m:02d}m"
        missing = []
        if not today.exists(): missing.append("today.md")
        if not weekly.exists(): missing.append("weekly.md")
        return False, f"MISSING: {', '.join(missing)}"
    elif dep == "queue_db":
        p = Path(QUEUE_DB)
        return p.exists(), str(p)
    elif dep == "tasks_table":
        p = Path(QUEUE_DB)
        if not p.exists():
            return False, "queue DB missing"
        try:
            import sqlite3
            con = sqlite3.connect(str(p))
            count = con.execute("SELECT COUNT(*) FROM tasks WHERE status='todo'").fetchone()[0]
            con.close()
            return True, f"{count} open tasks"
        except Exception as e:
            return False, str(e)
    elif dep == "outbox":
        p = Path(QUEUE_DB)
        if not p.exists():
            return False, "queue DB missing"
        try:
            import sqlite3
            con = sqlite3.connect(str(p))
            count = con.execute("SELECT COUNT(*) FROM outbox_items WHERE status='pending'").fetchone()[0]
            con.close()
            return True, f"{count} pending outbox items"
        except Exception as e:
            return False, str(e)
    elif dep == "vault":
        sys.path.insert(0, REPO)
        try:
            import aaka_config
            vp = aaka_config.vault_path_for("alex")
            return vp.exists(), str(vp)
        except Exception:
            return False, "vault path unknown"
    elif dep == "gmail_scope":
        return False, "not yet configured"
    return True, "assumed ok"

# ── Sensor local-intent table (not in registry) ──────────────────────────────
LOCAL_INTENTS = [
    ("today_schedule",  "/today", "0", "today.md", "Reads cached calendar file"),
    ("weekly_schedule", "/week",  "0", "weekly.md", "Reads cached calendar file"),
    ("health_check",    "/status","0", "audit.log", "Audit tail + file mtime"),
    ("menu",            "/menu",  "0", "—",         "Static command listing"),
    ("test_status",     "/tstatus","0","queue DB",   "Live queue + outbox snapshot"),
    ("flush_outbox",    "/outbox","0", "queue DB",   "Flush pending outbox to user"),
]

print(f"\n{BOLD}── Local (Sensor-only, 0 tokens) ──────────────────────────────{NC}")
for intent, trigger, tokens, dep, desc in LOCAL_INTENTS:
    print(f"\n  {BOLD}{intent}{NC}  [{trigger}]")
    print(f"    {desc}")
    print(f"    Tokens: {tokens}  |  Depends on: {dep}")
    # quick dep check
    if dep == "today.md":
        p = Path(CALENDAR_DIR) / "today.md"
        if p.exists():
            import time
            age = int(time.time() - p.stat().st_mtime)
            h, m = divmod(age // 60, 60)
            ok(f"today.md present, age {h}h{m:02d}m")
        else:
            warn("today.md missing — run calendar sync")
    elif dep == "weekly.md":
        p = Path(CALENDAR_DIR) / "weekly.md"
        ok("weekly.md present") if p.exists() else warn("weekly.md missing")
    elif dep == "audit.log":
        p = Path(LOGS_DIR) / "audit.log"
        ok("audit.log present") if p.exists() else warn("audit.log missing (will be created on first sensor run)")
    elif dep == "queue DB":
        p = Path(QUEUE_DB)
        ok(f"queue DB present") if p.exists() else fail(f"queue DB missing: {p}")

print(f"\n{BOLD}── Registry Skills ────────────────────────────────────────────{NC}")
for key, s in skills.items():
    enabled   = s.get("enabled", True)
    runs_on   = s.get("runs_on", ["executor"])
    intent    = s.get("intent", key)
    confirm   = s.get("requires_confirmation", False)
    entrypoint= s.get("entrypoint") or "(inline)"
    meta      = SKILL_META.get(key, {})

    status_icon = f"{GREEN}✓ enabled{NC}" if enabled else f"{YELLOW}⊘ disabled{NC}"
    runs_str = ", ".join(runs_on)
    avail_here = INSTANCE in runs_on or (INSTANCE == "local" and "executor" in runs_on)

    print(f"\n  {BOLD}{key}{NC}  [{status_icon}]")
    if meta.get("desc"):
        print(f"    {meta['desc']}")
    print(f"    Intent:   {intent}")
    print(f"    Trigger:  {meta.get('trigger', '—')}")
    print(f"    Tokens:   {meta.get('tokens', '?')}")
    print(f"    Runs on:  {runs_str}  {'← available here' if avail_here else '← NOT on this instance'}")
    print(f"    Confirm:  {'yes' if confirm else 'no'}")
    print(f"    File:     {entrypoint}")

    # Dependency checks
    deps = meta.get("deps", [])
    if deps:
        print(f"    Dependencies:")
        for dep in deps:
            ok_dep, detail = check_dep(dep)
            sym = f"  {GREEN}✓{NC}" if ok_dep else f"  {RED}✗{NC}"
            print(f"      {sym}  {dep}: {detail}")

# ── Calendar stats ───────────────────────────────────────────────────────────
print(f"\n{BOLD}── Calendar Stats ─────────────────────────────────────────────{NC}")
today_md = Path(CALENDAR_DIR) / "today.md"
weekly_md = Path(CALENDAR_DIR) / "weekly.md"
if today_md.exists():
    lines = today_md.read_text().splitlines()
    events = [l for l in lines if l.strip().startswith("-") or (l.strip() and l[0].isdigit())]
    info(f"today.md: ~{len(events)} entries")
else:
    warn("today.md not found")
if weekly_md.exists():
    lines = weekly_md.read_text().splitlines()
    events = [l for l in lines if l.strip().startswith("-") or (l.strip() and l[0].isdigit())]
    info(f"weekly.md: ~{len(events)} entries")
else:
    warn("weekly.md not found")

# ── Queue stats ──────────────────────────────────────────────────────────────
print(f"\n{BOLD}── Queue Stats ────────────────────────────────────────────────{NC}")
qp = Path(QUEUE_DB)
if qp.exists():
    import sqlite3
    con = sqlite3.connect(str(qp))
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) FROM queue_items GROUP BY status"
        ).fetchall()
        for status, count in rows:
            info(f"queue_items [{status}]: {count}")
        done = con.execute(
            "SELECT COUNT(*) FROM queue_items WHERE status='done'"
        ).fetchone()[0]
        intents = con.execute(
            "SELECT intent, COUNT(*) FROM queue_items WHERE status='done' GROUP BY intent ORDER BY 2 DESC"
        ).fetchall()
        if intents:
            info("Completed by intent:")
            for intent, count in intents:
                print(f"      {intent}: {count}")
    except Exception as e:
        warn(f"queue query failed: {e}")
    finally:
        con.close()
else:
    warn(f"queue DB not found: {qp}")

# ── Skill audit log ──────────────────────────────────────────────────────────
print(f"\n{BOLD}── Skill Audit (last 10) ───────────────────────────────────────{NC}")
audit_log = Path(LOGS_DIR) / "skills" / "skill-audit.log"
if audit_log.exists():
    lines = audit_log.read_text().splitlines()
    for line in lines[-10:]:
        print(f"    {line}")
else:
    warn(f"skill-audit.log not found at {audit_log}")
PYEOF

echo ""
info "Run 'bash admin/aaka.sh diagnose' for full system health."
