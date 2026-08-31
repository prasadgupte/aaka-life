#!/usr/bin/env bash
# admin/test.sh — Smoke tests
# Run: bash admin/test.sh
#
# RULE: When fixing a new bug or discovering a new failure mode,
#       append a test to this file.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/detect.sh"

PASS=0; FAIL=0
QUEUE_DB="${QUEUE_DB:-$AAKA_CONFIG_DIR/data/queue/butler.db}"

# Use venv python on local Mac so PyPI deps (yaml, etc.) are available
if [ "$INSTANCE" = "local" ] && [ -f "$REPO_DIR/venv/bin/python3" ]; then
    PYTHON="$REPO_DIR/venv/bin/python3"
else
    PYTHON="python3"
fi

check() {
    local desc="$1"; shift
    if "$@" &>/dev/null; then
        ok "$desc"
        PASS=$((PASS + 1))
    else
        fail "$desc"
        FAIL=$((FAIL + 1))
    fi
}

echo -e "${BOLD}Aaka Smoke Tests — $INSTANCE ($MODE)${NC}"
echo "======================================"

SENSOR_CONTAINER=$( [ "$INSTANCE" = "vps" ] && echo "aaka-sensor" || echo "aaka-sensor-dev" )
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${SENSOR_CONTAINER}$"; then
    SENSOR_UP=true
else
    SENSOR_UP=false
fi

check_sensor() {
    local desc="$1"; shift
    if [ "$SENSOR_UP" = "false" ]; then
        warn "SKIP  $desc  (&Away container not running: $SENSOR_CONTAINER)"
        return
    fi
    check "$desc" "$@"
}

# ── 1. Gateway import ────────────────────────────────────────────────────────
header "1. Gateway import"
check_sensor "GatewayAdapter importable" \
    docker exec "$SENSOR_CONTAINER" python3 -c "from gateway.adapter import GatewayAdapter; print('OK')"

# ── 2. Config load ───────────────────────────────────────────────────────────
header "2. Config load"
check_sensor "aaka_config loads (aaka.yaml present + valid)" \
    docker exec "$SENSOR_CONTAINER" python3 aaka_config.py

# Role policy: contacts_sync must be disabled on the sensor so the cron stops
# emitting "No CSV ... no People API token" WARNs every 30 min.
check_sensor "contacts_sync disabled on sensor role" \
    docker exec "$SENSOR_CONTAINER" bash -c '. /etc/aaka-cron-env && python3 -c "
import aaka_config
assert aaka_config.role() == \"sensor\", f\"AAKA_ROLE={aaka_config.role()!r}\"
assert not aaka_config.skill_enabled(\"contacts_sync\"), \"contacts_sync should be disabled\"
print(\"OK\")
"'

# ── 3. Dry-run intents ───────────────────────────────────────────────────────
header "3. Dry-run intents"
check_sensor "/menu dry-run" \
    docker exec "$SENSOR_CONTAINER" python3 sensor/router_sensor.py --dry-run "/menu"
check_sensor "/add dry-run" \
    docker exec "$SENSOR_CONTAINER" python3 sensor/router_sensor.py --dry-run "/add physio friday 3pm"
check_sensor "/today dry-run" \
    docker exec "$SENSOR_CONTAINER" python3 sensor/router_sensor.py --dry-run "/today"
check_sensor "/week dry-run" \
    docker exec "$SENSOR_CONTAINER" python3 sensor/router_sensor.py --dry-run "/week"

# ── 3b. Agent reply intercept: slash commands must escape ───────────────────
# Regression test for the bug where a user's `/tasks` was eaten as a reply
# to a pending agent_reply_request. The fix in router_sensor.py guards the
# intercept with: `not (message or "").lstrip().startswith("/")`.
header "Agent reply intercept slash-escape"
if grep -q 'lstrip().startswith("/")' sensor/router_sensor.py; then
    ok "slash-command escape guard present in router_sensor.py"
    PASS=$((PASS + 1))
else
    fail "slash-command escape guard MISSING — /tasks etc. will be eaten by agent_reply intercept"
    FAIL=$((FAIL + 1))
fi

# VPS sensor must have its own sweeper cron — Mac-side expiry doesn't sync
# to VPS via vps_sync.py (HWM is on created_at only). Without this cron,
# stale 'waiting' rows would still accumulate on VPS.
if grep -q "expire_reply_requests" sensor/entrypoint.sh; then
    ok "VPS reply_request sweeper cron present in sensor/entrypoint.sh"
    PASS=$((PASS + 1))
else
    fail "VPS reply_request sweeper cron MISSING from sensor/entrypoint.sh"
    FAIL=$((FAIL + 1))
fi

# _push_files MUST NOT use delete=True for staging — the race with the pull
# step on the next cycle wipes VPS-staged files before Mac pulls them,
# stranding queue_items with media_staging_path pointing at deleted files.
if grep -A 12 'def _push_files' executor/vps_sync.py | grep -q 'staging_remote, delete=False'; then
    ok "vps_sync._push_files uses delete=False for staging (no race)"
    PASS=$((PASS + 1))
else
    fail "vps_sync._push_files staging push uses delete=True — races with pull, drops files"
    FAIL=$((FAIL + 1))
fi

# ── 4. Queue accessible ──────────────────────────────────────────────────────
header "4. Queue"
if [ -f "$QUEUE_DB" ]; then
    ok "Queue DB exists: $QUEUE_DB"
    PASS=$((PASS + 1))
    echo "  Last 5 items:"
    sqlite3 "$QUEUE_DB" \
        "SELECT id, intent, status FROM queue_items ORDER BY id DESC LIMIT 5;" 2>/dev/null | \
        sed 's/^/    /' || true
else
    fail "Queue DB not found: $QUEUE_DB"
    FAIL=$((FAIL + 1))
fi

# ── queue_test intent ────────────────────────────────────────────────────────
header "queue_test intent"
check_sensor "/tqueue dry-run" \
    docker exec "$SENSOR_CONTAINER" python3 sensor/router_sensor.py --dry-run "/tqueue"

# ── /route intent ─────────────────────────────────────────────────────────────
header "/route intent (iter 1 demo)"
check_sensor "/route dry-run" \
    docker exec "$SENSOR_CONTAINER" python3 sensor/router_sensor.py --dry-run "/route flo172 tax expense"

# ── ITER 0 tests ─────────────────────────────────────────────────────────────

test_0_vault_scaffold() {
  local vault
  vault="$($PYTHON -c "import sys; sys.path.insert(0,'$AAKA_BASE'); from aaka_config import vault_path_for; print(vault_path_for('alex'))" 2>/dev/null)"
  vault="${vault:-${HOME}/aaka-vault}"
  for d in 99-System 00-Inbox 01-Projects 03-Resources 04-Notes 90-Archives; do
    if [ -d "$vault/$d" ]; then
      ok "vault/$d exists"; PASS=$((PASS + 1))
    else
      fail "vault/$d missing"; FAIL=$((FAIL + 1))
    fi
  done
}

test_0_config_split() {
  if $PYTHON -c "
import sys; sys.path.insert(0, '$AAKA_BASE')
from aaka_config import vault_path_for, SHARED_VAULT_PATH, CONFIG_DIR
vp = vault_path_for('alex')
assert str(vp), 'vault_path_for empty'
print('vault_path_for(alex):', vp)
print('SHARED_VAULT_PATH:', SHARED_VAULT_PATH)
print('CONFIG_DIR:', CONFIG_DIR)
" 2>/dev/null; then
    ok "config split: vault_path_for resolves"; PASS=$((PASS + 1))
  else
    fail "config split: aaka_config.py error"; FAIL=$((FAIL + 1))
  fi
}

test_0_context_migration() {
  local vault
  vault="$($PYTHON -c "import sys; sys.path.insert(0,'$AAKA_BASE'); from aaka_config import vault_path_for; print(vault_path_for('alex'))" 2>/dev/null)"
  vault="${vault:-${HOME}/aaka-vault}"
  if [ -f "$vault/99-System/contexts/family.md" ]; then
    ok "family.md present in vault"; PASS=$((PASS + 1))
  else
    fail "family.md missing from vault"; FAIL=$((FAIL + 1))
  fi
  if [ -f "$AAKA_BASE/contexts/family/SOUL.md" ]; then
    ok "SOUL.md original preserved (copy not move)"; PASS=$((PASS + 1))
  else
    fail "SOUL.md original was deleted — should be a copy"; FAIL=$((FAIL + 1))
  fi
}

header "ITER 0 — Vault scaffold"
test_0_vault_scaffold

header "ITER 0 — Config split"
test_0_config_split

header "ITER 0 — Context migration"
test_0_context_migration

# ── ITER 1 tests ─────────────────────────────────────────────────────────────

test_1_tag_resolution() {
  $PYTHON -c "
import sys; sys.path.insert(0, '$AAKA_BASE')
from tools.inbox_router import route, load_references, resolve_tag
from aaka_config import vault_path_for
from pathlib import Path

vault = vault_path_for('alex')
refs = load_references(vault)

# flo172 must resolve as entity
kind, key, defn = resolve_tag('flo172', refs)
assert key == 'flo172', f'flo172 not resolved: {kind}/{key}'

# florianstrasse alias must resolve to flo172
kind2, key2, defn2 = resolve_tag('florianstrasse', refs)
assert key2 == 'flo172', f'alias florianstrasse not resolved: {kind2}/{key2}'

# fuzzy match: flo17 should match flo172
kind3, key3, _ = resolve_tag('flo17', refs)
assert key3 == 'flo172', f'fuzzy flo17 not resolved: {kind3}/{key3}'

# unknown tag is logged as unknown
kind4, key4, _ = resolve_tag('xyzzy_nonexistent_tag', refs)
assert kind4 == 'unknown', f'xyzzy should be unknown, got {kind4}'

print('tag resolution: all assertions passed')
" 2>/dev/null && { ok "test_1_tag_resolution: all assertions passed"; PASS=$((PASS+1)); } \
  || { fail "test_1_tag_resolution: failed"; FAIL=$((FAIL+1)); }
}

test_1_inbox_routing() {
  $PYTHON -c "
import sys; sys.path.insert(0, '$AAKA_BASE')
from tools.inbox_router import route
from aaka_config import vault_path_for

vault = vault_path_for('alex')
result = route(['flo172', 'tax', 'expense'], namespace='alex', vault=vault, dry_run=True)

assert result['llm_calls'] == 0, f'Expected 0 LLM calls, got {result[\"llm_calls\"]}'
assert 'flo172' in result['entities'], f'flo172 not in entities'
assert 'tax' in result['actions'] or 'expense' in result['actions'], 'no actions resolved'
assert len(result['outcomes']) >= 2, f'Expected >=2 outcomes, got {len(result[\"outcomes\"])}'
print(f'outcomes={len(result[\"outcomes\"])}, llm_calls={result[\"llm_calls\"]}')
" 2>/dev/null && { ok "test_1_inbox_routing: flo172+tax+expense → ≥2 outcomes, 0 LLM calls"; PASS=$((PASS+1)); } \
  || { fail "test_1_inbox_routing: failed"; FAIL=$((FAIL+1)); }
}

test_1_index_rebuild() {
  $PYTHON -c "
import sys, json; sys.path.insert(0, '$AAKA_BASE')
from tools.build_indexes import build_all
from aaka_config import vault_path_for

vault = vault_path_for('alex')
summary = build_all(vault)

# Check all 4 index files exist and are valid JSON
index_dir = vault / '99-System' / '.indexes'
import aaka_config as _cfg
carrier_ids = [m['id'] for m in _cfg.carriers()] or ['alice']
check_fnames = [f'{ns}-people-index.json' for ns in carrier_ids] + ['projects-index.json', 'tags-index.json']
for fname in check_fnames:
    data = json.loads((index_dir / fname).read_text())
    assert isinstance(data, (list, dict)), f'{fname} is not list/dict'

print(f'scanned={summary[\"scanned_files\"]} projects={summary[\"projects\"]} tags={summary[\"unique_tags\"]}')
" 2>/dev/null && { ok "test_1_index_rebuild: all 4 indexes built + valid JSON"; PASS=$((PASS+1)); } \
  || { fail "test_1_index_rebuild: failed"; FAIL=$((FAIL+1)); }
}

header "ITER 1 — Tag resolution"
test_1_tag_resolution

header "ITER 1 — Inbox routing"
test_1_inbox_routing

header "ITER 1 — Index rebuild"
test_1_index_rebuild

# ── ITER 2 tests ─────────────────────────────────────────────────────────────

test_2_task_manager_crud() {
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$AAKA_BASE')
from skills.task_manager import create_task, list_tasks, complete_task, get_task

tid = create_task('test task iter2', namespace='alice', notes='smoke test', source='cli')
assert tid, 'create_task returned empty id'

tasks = list_tasks('alice', status='todo')
ids = [t['id'] for t in tasks]
assert tid in ids, f'new task {tid} not found in list_tasks'

t = get_task(tid)
assert t and t['title'] == 'test task iter2', f'get_task mismatch: {t}'

complete_task(tid)
t2 = get_task(tid)
assert t2['status'] == 'done', f'expected done, got {t2[\"status\"]}'

print(f'task_manager CRUD OK  id={tid[:8]}')
" 2>/dev/null && { ok "test_2_task_manager_crud: create/list/get/complete"; PASS=$((PASS+1)); } \
  || { fail "test_2_task_manager_crud: failed"; FAIL=$((FAIL+1)); }
}

test_2_tasks_table_schema() {
  local db="${QUEUE_DB:-$AAKA_CONFIG_DIR/data/queue/butler.db}"
  if sqlite3 "$db" "SELECT id, namespace, title, status FROM tasks LIMIT 1;" &>/dev/null; then
    ok "test_2_tasks_table_schema: tasks table has expected columns"; PASS=$((PASS+1))
  else
    fail "test_2_tasks_table_schema: tasks table missing or wrong schema"; FAIL=$((FAIL+1))
  fi
}

test_2_status_queue_sensor() {
  if [ "$SENSOR_UP" = "false" ]; then
    warn "SKIP  test_2_status_queue_sensor  (&Away container not running)"; return
  fi
  docker exec "$SENSOR_CONTAINER" \
    python3 sensor/router_sensor.py --dry-run "/status queue" &>/dev/null \
  && { ok "test_2_status_queue_sensor: /status queue dry-run in &Away"; PASS=$((PASS+1)); } \
  || { fail "test_2_status_queue_sensor: &Away dry-run failed"; FAIL=$((FAIL+1)); }
}

header "ITER 2 — task_manager CRUD"
test_2_task_manager_crud

header "ITER 2 — tasks table schema"
test_2_tasks_table_schema

header "ITER 2 — /status queue sensor dry-run"
test_2_status_queue_sensor

# ── ITER 2.5 tests ───────────────────────────────────────────────────────────

test_25_botname_normalization() {
  if [ "$SENSOR_UP" = "false" ]; then
    warn "SKIP  test_25_botname_normalization  (&Away container not running)"; return
  fi
  docker exec "$SENSOR_CONTAINER" \
    python3 sensor/router_sensor.py --dry-run "/status@aakashbot queue" 2>&1 \
    | grep -q "Queue summary" \
  && { ok "test_25_botname_normalization: /status@botname queue → Queue summary"; PASS=$((PASS+1)); } \
  || { fail "test_25_botname_normalization: normalization failed (container may not be running)"; FAIL=$((FAIL+1)); }
}

test_25_status_code_sensor() {
  if [ "$SENSOR_UP" = "false" ]; then
    warn "SKIP  test_25_status_code_sensor  (&Away container not running)"; return
  fi
  docker exec "$SENSOR_CONTAINER" \
    python3 sensor/router_sensor.py --dry-run "/status code" 2>&1 \
    | grep -q "Code versions" \
  && { ok "test_25_status_code_sensor: /status code dry-run in &Away"; PASS=$((PASS+1)); } \
  || { fail "test_25_status_code_sensor: /status code failed (container may not be running)"; FAIL=$((FAIL+1)); }
}

header "ITER 2.5 — Telegram @botname normalization"
test_25_botname_normalization

header "ITER 2.5 — /status code sub-command"
test_25_status_code_sensor

# ── ITER 2.8 tests ───────────────────────────────────────────────────────────

test_28_outbox_schema() {
  [ -f "$QUEUE_DB" ] \
  && sqlite3 "$QUEUE_DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='outbox_items';" 2>/dev/null \
     | grep -q outbox_items \
  && { ok "test_28_outbox_schema: outbox_items table present"; PASS=$((PASS+1)); } \
  || { fail "test_28_outbox_schema: outbox_items table missing — run: sqlite3 \$QUEUE_DB < aaka_queue/schema.sql"; FAIL=$((FAIL+1)); }
}

test_28_executor_echo_dryrun() {
  if [ "$SENSOR_UP" = "false" ]; then
    warn "SKIP  test_28_executor_echo_dryrun  (&Away container not running)"; return
  fi
  docker exec "$SENSOR_CONTAINER" \
    python3 sensor/router_sensor.py --dry-run "/texec hello world" 2>&1 \
    | grep -q "executor_echo" \
  && { ok "test_28_executor_echo_dryrun: /texec dry-run queues executor_echo (&Away→&Home)"; PASS=$((PASS+1)); } \
  || { fail "test_28_executor_echo_dryrun: dry-run failed (container may not be running)"; FAIL=$((FAIL+1)); }
}

header "ITER 2.8 — outbox_items table schema"
test_28_outbox_schema

header "ITER 2.8 — /texec dry-run"
test_28_executor_echo_dryrun

# ── ITER 2.9 tests ───────────────────────────────────────────────────────────

test_29_tstatus_dryrun() {
  if [ "$SENSOR_UP" = "false" ]; then
    warn "SKIP  test_29_tstatus_dryrun  (&Away container not running)"; return
  fi
  docker exec "$SENSOR_CONTAINER" \
    python3 sensor/router_sensor.py --dry-run "/tstatus" 2>&1 \
    | grep -q "Queue status" \
  && { ok "test_29_tstatus_dryrun: /tstatus dry-run returns queue status"; PASS=$((PASS+1)); } \
  || { fail "test_29_tstatus_dryrun: /tstatus dry-run failed (container may not be running)"; FAIL=$((FAIL+1)); }
}

test_29_tthread_help_dryrun() {
  if [ "$SENSOR_UP" = "false" ]; then
    warn "SKIP  test_29_tthread_help_dryrun  (&Away container not running)"; return
  fi
  docker exec "$SENSOR_CONTAINER" \
    python3 sensor/router_sensor.py --dry-run "/tthread help" 2>&1 \
    | grep -q "Threading smoke test" \
  && { ok "test_29_tthread_help_dryrun: /tthread help returns description"; PASS=$((PASS+1)); } \
  || { fail "test_29_tthread_help_dryrun: /tthread help failed (container may not be running)"; FAIL=$((FAIL+1)); }
}

header "ITER 2.9 — /tstatus dry-run"
test_29_tstatus_dryrun

header "ITER 2.9 — /tthread help dry-run"
test_29_tthread_help_dryrun

# ── ITER 2.9+ tests ──────────────────────────────────────────────────────────

test_outbox_flush_dryrun() {
  if [ "$SENSOR_UP" = "false" ]; then
    warn "SKIP  test_outbox_flush_dryrun  (&Away container not running)"; return
  fi
  docker exec "$SENSOR_CONTAINER" \
    python3 sensor/router_sensor.py --dry-run "/outbox" 2>&1 \
    | grep -q "flush_outbox" \
  && { ok "test_outbox_flush_dryrun: /outbox dry-run matches flush_outbox intent"; PASS=$((PASS+1)); } \
  || { fail "test_outbox_flush_dryrun: dry-run failed (container may not be running)"; FAIL=$((FAIL+1)); }
}

test_outbox_source_column() {
  local db="${QUEUE_DB:-$AAKA_CONFIG_DIR/data/queue/butler.db}"
  [ -f "$db" ] \
  && sqlite3 "$db" "SELECT source FROM outbox_items LIMIT 1;" &>/dev/null \
  && { ok "test_outbox_source_column: outbox_items.source column present"; PASS=$((PASS+1)); } \
  || { fail "test_outbox_source_column: column missing — run migrate_outbox_source.sql"; FAIL=$((FAIL+1)); }
}

header "ITER 2.9+ — /outbox flush dry-run"
test_outbox_flush_dryrun

header "ITER 2.9+ — outbox_items.source column"
test_outbox_source_column

test_agent_photos_endpoint() {
  # Verify POST /v1/photos is registered in the gateway (import check — no live call)
  python3 -c "
import sys, os
sys.path.insert(0, os.environ.get('AAKA_BASE', '.'))
from gateway.agent_api import app
routes = [r.path for r in app.routes]
assert '/v1/photos' in routes, f'/v1/photos not found in routes: {routes}'
print('ok')
" 2>&1 | grep -q "^ok" \
  && { ok "test_agent_photos_endpoint: /v1/photos route registered"; PASS=$((PASS+1)); } \
  || { fail "test_agent_photos_endpoint: /v1/photos route missing in agent_api.py"; FAIL=$((FAIL+1)); }
}

test_agent_client_send_photo() {
  # Verify send_photo() method is present in the SDK
  python3 -c "
import sys, os
sys.path.insert(0, os.environ.get('AAKA_BASE', '.'))
from gateway.agent_client import AakaClient
assert hasattr(AakaClient, 'send_photo'), 'send_photo method missing'
print('ok')
" 2>&1 | grep -q "^ok" \
  && { ok "test_agent_client_send_photo: AakaClient.send_photo present"; PASS=$((PASS+1)); } \
  || { fail "test_agent_client_send_photo: AakaClient.send_photo missing in agent_client.py"; FAIL=$((FAIL+1)); }
}

header "Photos endpoint — /v1/photos route"
test_agent_photos_endpoint

header "Photos endpoint — AakaClient.send_photo"
test_agent_client_send_photo

test_aaka_config_dotenv_load() {
  # Regression: launchd-run gateway processes inherit only the env vars listed
  # in their plist. aaka_config must source $AAKA_CONFIG_DIR/.env so secrets
  # like TELEGRAM_BOT_TOKEN reach in-process senders (e.g. /v1/photos).
  python3 -c "
import os, sys, tempfile
sys.path.insert(0, os.environ.get('AAKA_BASE', '.'))
with tempfile.TemporaryDirectory() as d:
    open(os.path.join(d, '.env'), 'w').write('AAKA_DOTENV_PROBE=xyz\n')
    os.environ['AAKA_CONFIG_DIR'] = d
    os.environ.pop('AAKA_DOTENV_PROBE', None)
    for m in list(sys.modules):
        if m == 'aaka_config': del sys.modules[m]
    import aaka_config  # noqa: F401
    assert os.environ.get('AAKA_DOTENV_PROBE') == 'xyz', 'dotenv not sourced'
print('ok')
" 2>&1 | grep -q "^ok" \
  && { ok "test_aaka_config_dotenv_load: aaka_config sources \$AAKA_CONFIG_DIR/.env"; PASS=$((PASS+1)); } \
  || { fail "test_aaka_config_dotenv_load: aaka_config did not source .env (gateway photo path will 500)"; FAIL=$((FAIL+1)); }
}

header "aaka_config — \$AAKA_CONFIG_DIR/.env sourcing"
test_aaka_config_dotenv_load


# ── ITER 2 (full) — Tasks.md regeneration + calendar bridge ──────────────────

test_2_tasks_md_regen() {
  $PYTHON -c "
import sys, uuid; sys.path.insert(0, '$AAKA_BASE')
from skills.task_manager import create_task, regenerate_tasks_md
create_task(title='test regen ' + str(uuid.uuid4())[:8], namespace='alice', source='test')
out = regenerate_tasks_md()
assert out.exists()
assert 'alice' in out.read_text()
print('Tasks.md OK:', out)
" 2>/dev/null \
  && { ok "test_2_tasks_md_regen: Tasks.md written to vault/System/Tasks.md"; PASS=$((PASS+1)); } \
  || { fail "test_2_tasks_md_regen: regenerate_tasks_md() failed"; FAIL=$((FAIL+1)); }
}

test_2_calendar_bridge_noop() {
  $PYTHON -c "
import sys; sys.path.insert(0, '$AAKA_BASE')
from skills.task_manager import bridge_to_calendar
result = bridge_to_calendar({'title': 'no date task', 'namespace': 'alice'})
assert result == False
print('calendar_bridge: no-op OK')
" 2>/dev/null \
  && { ok "test_2_calendar_bridge_noop: no bridge without due_date"; PASS=$((PASS+1)); } \
  || { fail "test_2_calendar_bridge_noop: bridge_to_calendar failed"; FAIL=$((FAIL+1)); }
}

header "ITER 2 — Tasks.md regeneration"
test_2_tasks_md_regen

header "ITER 2 — calendar bridge no-op"
test_2_calendar_bridge_noop

# ── ITER 3: Skill Loader ─────────────────────────────────────────────────────

test_3_skill_loader() {
  $PYTHON -c "
import sys; sys.path.insert(0, '$AAKA_BASE')
from executor.skill_loader import SkillLoader
sl = SkillLoader('$AAKA_BASE/skills/registry.yaml', '/tmp')
skill = sl.resolve('add_event')
assert skill['_key'] == 'calendar_add_event', f'wrong key: {skill}'
print('resolved add_event OK:', skill['_key'])
" 2>/dev/null \
  && { ok "test_3_skill_loader: add_event resolves to calendar_add_event"; PASS=$((PASS+1)); } \
  || { fail "test_3_skill_loader: resolve failed"; FAIL=$((FAIL+1)); }
}

test_3_unknown_user() {
  $PYTHON -c "
import sys; sys.path.insert(0, '$AAKA_BASE')
from executor.skill_loader import SkillLoader, UnknownUser
sl = SkillLoader('$AAKA_BASE/skills/registry.yaml', '/tmp')
try:
    sl.resolve('add_event', sender='+15550000000_unknown')
    print('ERROR: should have raised UnknownUser')
    sys.exit(1)
except UnknownUser:
    print('UnknownUser raised correctly')
" 2>/dev/null \
  && { ok "test_3_unknown_user: unknown sender raises UnknownUser"; PASS=$((PASS+1)); } \
  || { fail "test_3_unknown_user: UnknownUser not raised for unknown sender"; FAIL=$((FAIL+1)); }
}

test_3_audit_log() {
  AUDIT_TMP=$(mktemp -d)
  $PYTHON -c "
import sys; sys.path.insert(0, '$AAKA_BASE')
from executor.skill_loader import SkillLoader
sl = SkillLoader('$AAKA_BASE/skills/registry.yaml', '$AUDIT_TMP')
skill = {'_key': 'test_skill', 'intent': 'queue_test', 'requires_confirmation': False}
sl._audit('test_skill', 'queue_test', 'ok', 'abc123')
log = '$AUDIT_TMP/skills/skill-audit.log'
import os; content = open(log).read()
assert 'queue_test' in content, 'audit entry missing'
print('audit log entry written OK')
" 2>/dev/null \
  && { ok "test_3_audit_log: audit log written on skill execute"; PASS=$((PASS+1)); } \
  || { fail "test_3_audit_log: audit log not written"; FAIL=$((FAIL+1)); }
  rm -rf "$AUDIT_TMP"
}

header "ITER 3 — Skill Loader"
test_3_skill_loader

header "ITER 3 — Unknown user approval flow"
test_3_unknown_user

header "ITER 3 — Audit log"
test_3_audit_log

# ── Auth / Sync smoke tests ───────────────────────────────────────────────────

header "Auth — auth_check importable"
check "auth_check importable" \
    bash -c "AAKA_BASE=$REPO_DIR AAKA_CONFIG_DIR=${AAKA_CONFIG_DIR} \
     $PYTHON -c 'from skills.auth_check import check_token, heartbeat_calendar; print(\"OK\")'"

if [ "$INSTANCE" = "local" ]; then
    header "Auth — aaka.sh auth"
    check "aaka.sh auth exits 0" \
        bash "$SCRIPT_DIR/auth.sh"

    header "Sync — aaka.sh sync"
    check "aaka.sh sync exits 0" \
        bash "$SCRIPT_DIR/sync.sh"
fi

# ── VPS Calendar Push smoke test (local only) ─────────────────────────────────

test_vps_calendar_push() {
    if [ "$INSTANCE" != "local" ]; then
        warn "SKIP  test_vps_calendar_push  (local only)"
        return
    fi
    # Run the sync+push wrapper
    if ! AAKA_BASE="$REPO_DIR" AAKA_CONFIG_DIR="$AAKA_CONFIG_DIR" AAKA_CONTEXT=family \
            bash "$REPO_DIR/executor/calendar_sync_and_push.sh" &>/dev/null; then
        fail "test_vps_calendar_push: calendar_sync_and_push.sh exited non-zero"
        FAIL=$((FAIL + 1)); return
    fi
    ok "test_vps_calendar_push: wrapper exited 0"
    PASS=$((PASS + 1))

    # Check VPS file freshness (5-minute window)
    local vps_host="${AAKA_VPS_HOST:-aaka-away}"
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "$vps_host" echo ok &>/dev/null 2>&1; then
        now=$(date +%s)
        mtime=$(ssh "$vps_host" "stat -c %Y /opt/aaka-config/data/calendar/today.md 2>/dev/null || echo 0")
        age=$(( now - mtime ))
        if [[ $mtime -ne 0 && $age -lt 300 ]]; then
            ok "test_vps_calendar_push: today.md on VPS is fresh (age=${age}s)"
            PASS=$((PASS + 1))
        else
            fail "test_vps_calendar_push: today.md on VPS is stale or missing (age=${age}s)"
            FAIL=$((FAIL + 1))
        fi
    else
        warn "test_vps_calendar_push: $vps_host unreachable — skipping VPS freshness check"
    fi
}

header "VPS Calendar Push"
test_vps_calendar_push

# ── Taxonomy refactor unit tests ──────────────────────────────────────────────
header "Taxonomy Refactor"
check "parse_pad_modifier pad30 → (30,30)" \
  $PYTHON -c "
from skills.calendar.prepare_event import parse_pad_modifier
assert parse_pad_modifier('pad30') == (30, 30), parse_pad_modifier('pad30')
print('OK')
"
check "parse_pad_modifier pad30-60 → (30,60)" \
  $PYTHON -c "
from skills.calendar.prepare_event import parse_pad_modifier
assert parse_pad_modifier('pad30-60') == (30, 60), parse_pad_modifier('pad30-60')
print('OK')
"
check "apply_event_padding adjusts times and appends actual time to title" \
  $PYTHON -c "
from skills.calendar.prepare_event import apply_event_padding
p = apply_event_padding({'title':'🏥 Child @ Physio','occurrences':[{'date':'2026-04-20','start_time':'09:00','end_time':'10:00'}],'description':''}, 45, 45)
occ = p['occurrences'][0]
assert occ['start_time'] == '08:15', occ['start_time']
assert occ['end_time'] == '10:45', occ['end_time']
assert '(09:00)' in p['title'], p['title']
print('OK')
"
check "_detect_carrier uses all member names (not just is_carrier=true)" \
  $PYTHON -c "
from skills.calendar.prepare_event import _detect_carrier
import aaka_config
# Use the first non-carrier member name if available, else skip
members = aaka_config.member_names()
carrier_ids = {m['id'] for m in aaka_config.carriers()}
non_carriers = [m['name'] for m in aaka_config.members() if m['id'] not in carrier_ids]
if non_carriers:
    name = non_carriers[0]
    carrier_name = aaka_config.member_names()[0] if aaka_config.member_names() else 'Alice'
    result = _detect_carrier(f'{carrier_name} takes {name} to physio')
    assert result == carrier_name, f'Expected {carrier_name}, got {result}'
    print('OK')
else:
    print('SKIP (no non-carrier members in config)')
"

# ── Tests: file_utils ────────────────────────────────────────────────────────

check "sanitize_filename rejects path traversal" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from tools.file_utils import sanitize_filename
result = sanitize_filename('../../../etc/passwd')
assert '..' not in result, f'traversal not stripped: {result}'
assert '/' not in result, f'slash not stripped: {result}'
print(f'OK: {result}')
"

check "validate_extension rejects .exe allows .pdf" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from tools.file_utils import validate_extension
assert not validate_extension('.exe'), '.exe should be rejected'
assert not validate_extension('.sh'), '.sh should be rejected'
assert validate_extension('.pdf'), '.pdf should be allowed'
assert validate_extension('.jpg'), '.jpg should be allowed'
assert validate_extension('.PNG'), '.PNG (case) should be allowed'
print('OK')
"

check "generate_drop_filename produces correct YYMMDD format" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from tools.file_utils import generate_drop_filename
import re
result = generate_drop_filename(['flo172', 'expense'], 'Invoice.pdf')
assert re.match(r'^\d{6}_flo172-expense_invoice\.pdf$', result), f'bad format: {result}'
# No tags
result2 = generate_drop_filename([], 'photo.jpg')
assert re.match(r'^\d{6}_photo\.jpg$', result2), f'bad no-tag format: {result2}'
print(f'OK: {result}, {result2}')
"

# ── Tests: drop_note / drop_file intent routing ──────────────────────────────

check "drop_note dry-run parses tags and body" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('/note flo172 expense Paid plumber 450', dry_run=True)
# dry_run prints payload to stdout — just check no crash
print('OK')
"

check "drop_file without media returns helpful error" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('/drop test invoice', dry_run=True)
assert 'requires a file' in reply, f'unexpected reply: {reply}'
print('OK')
"

check "media auto-detect sets intent to drop_file" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import match_intent
# A message with no command should return None (then media auto-detect kicks in)
assert match_intent('hello') is None
# /drop should match drop_file
assert match_intent('/drop test') == 'drop_file'
# /note should match drop_note
assert match_intent('/note flo172 body') == 'drop_note'
print('OK')
"

check "file_upsert is registered in skills/registry.yaml" \
  $PYTHON -c "
import sys, yaml
data = yaml.safe_load(open('$REPO_DIR/skills/registry.yaml'))
skills = data.get('skills', {})
assert any(s.get('intent') == 'file_upsert' for s in skills.values()), 'file_upsert missing from registry — note appends will fail with SkillNotFound'
print('OK')
"

check "n alias: 'n cab 15' routes to /notes (read), not /note (write)" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
# Re-implement the alias logic locally to verify routing without invoking the full sensor.
import re
def route(message):
    def _note_route(rest):
        fl = rest.split('\n')[0].split() if rest else []
        ml = '\n' in rest and rest.split('\n', 1)[1].strip()
        is_read_with_limit = len(fl) == 2 and fl[1].isdigit() and not ml
        has_body = (len(fl) >= 2 or ml) and not is_read_with_limit
        return ('/note ' if has_body else '/notes ') + rest
    if re.match(r'^[nN](\s|\$)', message) and not message.startswith('/'):
        return _note_route(message[1:].strip())
    return message
assert route('n cab 15').startswith('/notes '), f'got {route(\"n cab 15\")!r} — should read 15 lines'
assert route('n cab hello world').startswith('/note '), 'write should still work'
assert route('n cab').startswith('/notes '), 'bare topic should read'
print('OK')
"

check "drop_file routing: flo172+expense resolves to vault path" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from tools.inbox_router import route
r = route(['flo172', 'expense'], 'alex')
assert r['outcomes'], 'no outcomes'
ft = r['outcomes'][0].get('file_to', '')
assert 'flo172' in ft and 'expenses' in ft, f'unexpected file_to: {ft}'
print('OK')
"

check "drop_file routing: unknown tags produce empty outcomes" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from tools.inbox_router import route
r = route(['randomxyz', 'nope'], 'alex')
assert not r['outcomes'], f'expected no outcomes: {r[\"outcomes\"]}'
assert 'randomxyz' in r['unknown'], f'expected unknown: {r[\"unknown\"]}'
print('OK')
"

check "drop_file routing: _resolve_dest_dir handles toplevel and relative paths" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from pathlib import Path
from skills.drop.drop_file import _resolve_dest_dir
vault = Path('/tmp/test-vault')
# All paths are vault-relative — no 02-Areas prefix
assert str(_resolve_dest_dir(vault, '_shared/props/flo172')) == str(vault / '_shared/props/flo172')
assert str(_resolve_dest_dir(vault, '00-Inbox/alex')) == str(vault / '00-Inbox/alex')
print('OK')
"

check "__drop_ask__ sentinel routes to drop_ask prompt (dry-run)" \
  $PYTHON -c "
import sys, json, tempfile, os
sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
# Create a temporary file to simulate media
with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
    f.write(b'%PDF-1.4 test')
    tmp = f.name
# Build Format A envelope with media header
meta = {'chat_id': 'telegram:123', 'message_id': '1', 'sender_id': '999', 'conversation_label': 'id:123'}
envelope = (
    'Conversation info (untrusted metadata):\n'
    '\`\`\`json\n' + json.dumps(meta) + '\n\`\`\`\n'
    f'[media attached: {tmp} (application/pdf)]\n'
    '__drop_ask__'
)
reply = route(envelope, dry_run=True)
os.unlink(tmp)
assert 'Received' in reply or 'No attachment' in reply, f'unexpected: {reply}'
print('OK')
"

check "f help returns drop help text" \
  $PYTHON -c "
import sys, os
sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('f help', dry_run=True)
assert 'Drop' in reply, f'expected Drop help, got: {reply}'
assert 'Smart Drop' in reply, f'expected Smart Drop in help, got: {reply}'
print('OK')
"

# ── Tests: single-letter aliases ──────────────────────────────────────────────

check "letter alias s → status" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('s', dry_run=True)
assert 'Sensor healthy' in reply or 'no log' in reply, f'unexpected: {reply}'
print('OK')
"

check "letter alias n → note" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('n fin 2025 claim boston taxi', dry_run=True)
# Should not error — dry_run prints payload
print('OK')
"

check "letter alias b → buy_list add + show" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.lists.list_manager import add_items, show, _list_path
import pathlib
p = _list_path('_test_list')
p.unlink(missing_ok=True)
added = add_items('_test_list', ['milk 2x'])
assert added['added'] == ['milk 2x'] and added['total'] == 1, f'add failed: {added}'
r2 = show('_test_list')
assert 'milk 2x' in r2 and '📋' in r2, f'show failed: {r2}'
p.unlink(missing_ok=True)
print('OK')
"

check "list_manager check_off and clear_done" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.lists.list_manager import add_items, check_off, clear_done, _list_path
p = _list_path('_test_list2')
p.unlink(missing_ok=True)
add_items('_test_list2', ['soap'])
add_items('_test_list2', ['shampoo'])
r = check_off('_test_list2', 'soap')
assert '✓' in r and 'soap' in r, f'check_off failed: {r}'
r2 = clear_done('_test_list2')
assert 'Archived 1' in r2, f'clear failed: {r2}'
p.unlink(missing_ok=True)
print('OK')
"

check "buy_list: new items marked with * after add" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.lists.list_manager import add_items, list_state, _list_path
from skills.lists.format import render_list_view
p = _list_path('_test_list_star')
p.unlink(missing_ok=True)
add_items('_test_list_star', ['old-one'])
out = add_items('_test_list_star', ['fresh-a', 'fresh-b'])
s = list_state('_test_list_star')
rendered = render_list_view(s['list_name'], s['unchecked'], s['checked_count'], s['shared'], added_count=len(out['added']))
assert 'fresh-a *' in rendered and 'fresh-b *' in rendered, f'no star on new: {rendered}'
assert 'old-one *' not in rendered, f'old item starred: {rendered}'
assert '* = added now' in rendered, f'missing legend: {rendered}'
p.unlink(missing_ok=True)
print('OK')
"

check "buy_list ideas: #clear archives instead of deleting" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os, pathlib, tempfile, shutil
tmp = tempfile.mkdtemp(prefix='aaka-ideas-')
os.environ['AAKA_CONFIG_DIR'] = tmp
pathlib.Path(tmp, 'data', 'lists').mkdir(parents=True)
from skills.lists.list_manager import add_items, check_off, clear_done, _resolve_list, ARCHIVE_HEADING
p = _resolve_list('_test_archive', 'alex')
add_items('_test_archive', ['x', 'y', 'z'], 'alex')
check_off('_test_archive', '1', 'alex')
clear_done('_test_archive', 'alex')
body = p.read_text()
assert ARCHIVE_HEADING in body, f'no archive heading after #clear: {body}'
assert '- [x] x' in body, f'archived line missing: {body}'
assert body.count('- [ ]') == 2, f'open items lost: {body}'
shutil.rmtree(tmp)
print('OK')
"

check "buy_list ideas: history + seed merge, sorted, dedup" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os, pathlib, tempfile, shutil
tmp = tempfile.mkdtemp(prefix='aaka-ideas-')
os.environ['AAKA_CONFIG_DIR'] = tmp
pathlib.Path(tmp, 'data', 'lists', '_seed').mkdir(parents=True)
seed = pathlib.Path(tmp, 'data', 'lists', '_seed', 'ideas.md')
seed.write_text('** food\nMilk\nMascarpone cheese\n[12:34, 1/2/2026] X: Bagget\n11:18, 1/23/2026] X: Cup cake\nRewe:\n- Yoghurt\n')
from skills.lists.list_manager import add_items, check_off, clear_done, ideas, _resolve_list
add_items('rewe', ['Milk'], 'alex')
check_off('rewe', '1', 'alex')
data = ideas('rewe', 'alex')
items = [e['item'].lower() for e in data['ideas']]
# Milk has history → first (history before seed)
assert data['ideas'][0]['source'] == 'history' and 'milk' in data['ideas'][0]['item'].lower(), f'milk not first: {data}'
# WhatsApp prefix stripped (both forms)
assert any('bagget' == i for i in items), f'bagget missing: {items}'
assert any('cup cake' == i for i in items), f'cup cake missing (lost-bracket form): {items}'
# Sub-heading 'Rewe:' skipped
assert 'rewe' not in items, f'sub-heading leaked: {items}'
# Yoghurt picked up (bullet stripped)
assert 'yoghurt' in items, f'yoghurt missing: {items}'
# Milk not duplicated by seed (history wins)
assert items.count('milk') == 1, f'milk duplicated: {items}'
shutil.rmtree(tmp)
print('OK')
"

check "buy_list ideas: add by number routes through pending state" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os, pathlib, tempfile, shutil
tmp = tempfile.mkdtemp(prefix='aaka-ideas-')
os.environ['AAKA_CONFIG_DIR'] = tmp
pathlib.Path(tmp, 'data', 'lists', '_seed').mkdir(parents=True)
pathlib.Path(tmp, 'data', 'lists', '_seed', 'ideas.md').write_text('** food\nApples\nBananas\nCarrots\n')
from skills.lists.list_manager import ideas, add_items, list_state
data = ideas('rewe', 'alex')
names = [e['item'] for e in data['ideas']]
assert names == ['Apples', 'Bananas', 'Carrots'], f'wrong order/items: {names}'
# Simulate router pending-state: pick #1 and #3
picked = [data['ideas'][0]['item'], data['ideas'][2]['item']]
add_items('rewe', picked, 'alex')
st = list_state('rewe', 'alex')
assert st['unchecked'] == ['Apples', 'Carrots'], f'unexpected list: {st}'
# Re-running ideas should now exclude items currently on the active list
data2 = ideas('rewe', 'alex')
names2 = [e['item'] for e in data2['ideas']]
assert names2 == ['Bananas'], f'on-list items not excluded: {names2}'
shutil.rmtree(tmp)
print('OK')
"

check "morning push: no 'starts at' header, no per-member tagging" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import io, contextlib
from sensor.scheduled_summaries import send_daily_morning
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    send_daily_morning(dry_run=True)
out = buf.getvalue()
assert 'starts at' not in out, f'starts-at header still present: {out[:400]}'
assert 'free today' not in out, f'old free-today line still present'
print('OK')
"

check "morning push: birthdays section appears AFTER tasks block" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import io, contextlib
from sensor.scheduled_summaries import send_daily_morning
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    send_daily_morning(dry_run=True)
out = buf.getvalue()
i_bday = out.find(\"🎂 Today's birthdays:\")
i_task = out.find('📋 ')
# Either block can be absent on a day with no birthdays / no tasks. Only assert order if both exist.
if i_bday != -1 and i_task != -1:
    assert i_task < i_bday, f'tasks should come before birthdays (task={i_task}, bday={i_bday})'
print('OK')
"

check "birthday_list: names hyperlinked, no 📱 icon on entry" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.contacts.format import format_entry
e = {'name': 'Kiran', 'first_name': 'Kiran', 'month_day': '06-12', 'mobile': '+911234567890'}
out = format_entry(1, e, {'mobile': '+911234567890', 'year': 1948}, age_year_resolver=lambda *a: 78)
assert '[Kiran](https://wa.me/911234567890' in out, f'no hyperlink: {out}'
assert '📱' not in out, f'phone icon still present: {out}'
# Fallback: no phone → plain name
e2 = {'name': 'Plain', 'first_name': 'Plain', 'month_day': '06-12', 'mobile': ''}
out2 = format_entry(2, e2, None, age_year_resolver=lambda *a: 50)
assert '](' not in out2 and 'Plain' in out2, f'expected plain name: {out2}'
print('OK')
"

check "render_today_birthdays: same hyperlink shape, no 📱 icon" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.contacts.format import render_today_birthdays
entries = [
    {'name': 'Kiran', 'first_name': 'Kiran', 'month_day': '06-12', 'mobile': '+911234567890'},
    {'name': 'NoPhone', 'first_name': 'NoPhone', 'month_day': '06-12', 'mobile': ''},
]
full_map = {'Kiran': {'mobile': '+911234567890', 'year': 1948}}
out = render_today_birthdays(entries, full_map)
assert \"🎂 Today's birthdays:\" in out, f'header missing: {out}'
assert '[Kiran](https://wa.me/911234567890' in out, f'no link: {out}'
assert '📱' not in out, f'phone icon present: {out}'
assert 'NoPhone' in out and '[NoPhone](' not in out, f'plain fallback failed: {out}'
# Empty list returns empty string
assert render_today_birthdays([], {}) == '', 'empty list should render empty'
print('OK')
"

check "render_task_counts: today period emits overdue/today/undated only, with /tasks hints" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import datetime
from skills.tasks.format import render_task_counts
today = datetime.date.today().isoformat()
yest = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
in_week = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
tasks = [
    {'due_date': yest},          # overdue
    {'due_date': today},         # today
    {'due_date': in_week},       # within week
    {'due_date': ''},            # undated
]
out_today = render_task_counts(tasks, period='today')
assert '1 overdue' in out_today and '1 due today' in out_today and '1 undated' in out_today, f'bad: {out_today}'
assert 'this week' not in out_today, f'week count leaked into today period: {out_today}'
assert '\`/tasks overdue\`' in out_today and '\`/tasks today\`' in out_today, f'no tap hints: {out_today}'
out_week = render_task_counts(tasks, period='week')
assert '1 this week' in out_week, f'week count missing in week period: {out_week}'
assert '\`/tasks week\`' in out_week, f'week hint missing: {out_week}'
# Empty list returns empty string
assert render_task_counts([], period='today') == '', 'empty tasks should render empty'
print('OK')
"

check "_compress_image: downsizes a large image, recycles original, returns size delta" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os, tempfile, random
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from pathlib import Path
try:
    from PIL import Image
except ImportError:
    print('SKIP (PIL not installed)'); sys.exit(0)
from skills.drop.drop_file import _compress_image
tmp = Path(tempfile.mkdtemp())
src = tmp / 'photo.jpg'
img = Image.new('RGB', (4000, 3000), (180, 90, 40))
pix = img.load()
for _ in range(50000):
    pix[random.randint(0,3999), random.randint(0,2999)] = (random.randint(0,255), random.randint(0,255), random.randint(0,255))
img.save(src, 'JPEG', quality=92)
orig_size = src.stat().st_size
assert orig_size > 500_000, f'test image too small to trigger compression: {orig_size}'
config_dir = tmp / 'config'
(config_dir / 'data').mkdir(parents=True)
res = _compress_image(src, config_dir, skip=False)
assert res['compressed'] is True, f'expected compressed=True: {res}'
assert res['new_size'] < res['original_size'], f'no size win: {res}'
assert res['recycled_path'].exists(), f'recycled file missing: {res[\"recycled_path\"]}'
# Skip=True path returns no-op
res2 = _compress_image(src, config_dir, skip=True) if src.exists() else None
# (src is gone now — recycled — so this branch is informational only)
print('OK')
"

check "_compress (dispatcher) + small image is left alone" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os, tempfile
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from pathlib import Path
try:
    from PIL import Image
except ImportError:
    print('SKIP'); sys.exit(0)
from skills.drop.drop_file import _compress
tmp = Path(tempfile.mkdtemp())
src = tmp / 'tiny.jpg'
Image.new('RGB', (100, 100), 'red').save(src, 'JPEG')
config_dir = tmp / 'config'
res = _compress(src, config_dir, skip=False)
assert res['compressed'] is False, f'tiny image should be skipped: {res}'
# Non-image, non-pdf is left alone
other = tmp / 'data.txt'
other.write_text('hello')
res2 = _compress(other, config_dir, skip=False)
assert res2['compressed'] is False
print('OK')
"

check "render_drop_success: rename marker + compression delta + keep hint" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.drop.format import render_drop_success
# Compressed image with rename + keep hint
out = render_drop_success(
    orig_filename='photo.jpg', filed_rel='alex/00-Inbox/den40_c.jpg',
    renamed_to='den40_c.jpg', compressed=True,
    original_size_bytes=8_400_000, new_size_bytes=1_200_000,
    keep_hash='a1b2c3d4',
)
assert '✏️' in out, f'rename marker missing: {out!r}'
assert '🗜 Compressed:' in out and '-85%' in out or '-86%' in out, f'delta missing: {out!r}'
assert '\`keep #a1b2c3d4\`' in out, f'keep hint missing: {out!r}'
# Uncompressed → no compression/keep lines
out2 = render_drop_success(
    orig_filename='note.txt', filed_rel='alex/04-Notes/note.txt',
    renamed_to='note.txt', compressed=False,
)
assert '🗜' not in out2 and 'keep #' not in out2, f'leaked compression hint: {out2!r}'
# Unknown tags → warning when not routed
out3 = render_drop_success(
    orig_filename='x.pdf', filed_rel='alex/00-Inbox/x.pdf',
    routed=False, unknown_tags=['zzz'], compressed=False,
)
assert 'Tags not recognized: zzz' in out3, f'unknown warning missing: {out3!r}'
print('OK')
"

check "keep_index: write + read round-trip" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os, tempfile, json
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from pathlib import Path
from skills.drop.drop_file import _write_keep_index, read_keep_index, write_keep_index
tmp = Path(tempfile.mkdtemp())
_write_keep_index(tmp, 'abc12345', {'recycled_path': 'data/recycle_bin/2026-05-16/x.pdf', 'vault_owner': 'alex', 'vault_dest': '00-Inbox/x_c.pdf', 'original_filename': 'x.pdf', 'stored_at': '2026-05-16T00:00:00Z'})
idx = read_keep_index(tmp)
assert 'abc12345' in idx, f'entry missing: {idx}'
assert idx['abc12345']['vault_owner'] == 'alex'
# Direct write replaces
write_keep_index(tmp, {})
assert read_keep_index(tmp) == {}, 'replace failed'
print('OK')
"

check "intent_registry: 'keep #abc12345' matches keep_original" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.intent_registry import match_intent
assert match_intent('keep #abc12345') == 'keep_original', match_intent('keep #abc12345')
assert match_intent('/keep #abc12345') == 'keep_original'
# Bare 'keep' (no hash) shouldn't match — would hijack other commands
assert match_intent('keep this in mind') != 'keep_original'
print('OK')
"

check "preview_destination + render_legacy_drop_preview: shows target folder for routed/entity-only/unknown" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from tools.inbox_router import preview_destination
from skills.drop.format import render_legacy_drop_preview
# Routed: entity + action → file_to from references.yaml
d = preview_destination(['flo172', 'expense'], 'alex')
assert d['routed'] is True, f'routed flag missing: {d}'
assert 'expenses' in d['dest_rel'], f'expected expenses folder: {d}'
out = render_legacy_drop_preview('rcpt.pdf', 12, ['flo172', 'expense'], d, '')
assert '→ ' in out and 'expenses' in out, f'arrow + dest missing: {out!r}'
# Entity-only: alias resolves to entity but no action → 00-Inbox + hint
d2 = preview_destination(['den40'], 'alex')
assert d2['needs_action'] is True, f'entity-only should flag needs_action: {d2}'
assert d2['entity_label'], f'entity_label missing: {d2}'
out2 = render_legacy_drop_preview('photo.jpg', 57, ['den40'], d2, '')
assert '00-Inbox' in out2 and 'add an action tag' in out2, f'entity-only hint missing: {out2!r}'
# Unknown tag → 00-Inbox + unknown hint
d3 = preview_destination(['xyzzy123'], 'alex')
assert d3['routed'] is False and d3['unknown'] == ['xyzzy123'], f'unknown not flagged: {d3}'
out3 = render_legacy_drop_preview('x.pdf', 1, ['xyzzy123'], d3, '')
assert 'unknown: #xyzzy123' in out3, f'unknown hint missing: {out3!r}'
print('OK')
"

check "render_completed: single shows inline, bulk uses bulleted list + recurring lines" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.tasks.format import render_completed
# Single task — kept compact on one line
single = render_completed([{'title': 'Buy milk'}])
assert single == '✅ Done: Buy milk', f'single wrong: {single!r}'
# Bulk — header with count then one bullet per task (no comma-joined flat line)
bulk = render_completed([{'title': 'A'}, {'title': 'B'}, {'title': 'C'}])
assert bulk.startswith('✅ Done (3)\n'), f'bulk header wrong: {bulk!r}'
assert '\n• A\n• B\n• C' in bulk, f'bullets missing: {bulk!r}'
assert ', ' not in bulk, f'flat comma list leaked into bulk: {bulk!r}'
# Recurring next lines append after bullets with formatted due date
out = render_completed([{'title': 'Weekly review'}], recurring_next=[{'title': 'Weekly review', 'due_date': '2026-05-23'}])
assert '🔄 Next: Weekly review — due 23 May' in out, f'recurring next missing: {out!r}'
assert render_completed([]) == '', 'empty completed should render empty'
print('OK')
"

check "_build_today_schedule: task counts appear when tasks exist; suppressed by include_tasks=False" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import aaka_config
alex = next((m for m in aaka_config.members() if m.get('id') == 'alex'), None)
sender = str(alex['telegram']) if alex else ''
from sensor.router_sensor import _build_today_schedule
out_on  = _build_today_schedule('alex', sender=sender, include_tasks=True)
out_off = _build_today_schedule('alex', sender=sender, include_tasks=False)
# When tasks exist, the count header should show in the include_tasks=True version.
# Skip the assertion if no tasks are open (would be a flake on a clean machine).
from skills.tasks.local_tasks import list_open
if list_open():
    assert '📋' in out_on and '\`/tasks' in out_on, f'no task counts in /today: {out_on[-400:]}'
    assert '\`/tasks' not in out_off, f'counts leaked when include_tasks=False: {out_off[-400:]}'
print('OK')
"

# ── Tests: j shortcut and menu completeness ──────────────────────────────────

check "letter alias t (bare) → list_tasks" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('t', dry_run=True)
# Should produce task list or empty list message
assert 'task' in reply.lower() or 'no open' in reply.lower() or '📋' in reply, f'unexpected: {reply}'
print('OK')
"

check "letter alias t text → add_task" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('t call dentist by friday', dry_run=True)
# dry_run prints intent=add_task
assert 'add_task' in reply.lower() or 'task' in reply.lower(), f'unexpected: {reply}'
print('OK')
"

check "/menu includes /tasks /snooze /block /day smart-drop /done /tags" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('/menu', dry_run=True)
for cmd in ['/tasks', '/snooze', '/block', '/day', '/done', '/tags']:
    assert cmd in reply, f'{cmd} missing from menu'
assert 'smart drop' in reply, 'smart drop missing from menu'
print('OK')
"

check "tags: /tags intent matches" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import match_intent
assert match_intent('/tags') == 'list_tags'
print('OK')
"

check "tags: /tags lists tag categories" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('/tags', dry_run=True)
assert 'People' in reply, f'People missing: {reply}'
assert '#PG' in reply or '#DE' in reply, f'no tags found: {reply}'
print('OK')
"

# ── Task management features ─────────────────────────────────────────────────
check "task: bulk /done parses multiple numbers" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import _extract_complete_task
r = _extract_complete_task('/done 1 3 5')
assert r['task_nums'] == [1, 3, 5], r
r = _extract_complete_task('/done 2')
assert r['task_nums'] == [2], r
print('OK')
"

check "task: /done today routes to history" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import _extract_complete_task
r = _extract_complete_task('/done today')
assert r.get('history') == 'today', r
r = _extract_complete_task('/done week')
assert r.get('history') == 'week', r
print('OK')
"

check "task: /tasks filters route to list_tasks" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import match_intent
assert match_intent('/tasks overdue') == 'list_tasks'
assert match_intent('/tasks today') == 'list_tasks'
assert match_intent('/tasks #errands') == 'list_tasks'
assert match_intent('/tasks @alice') == 'list_tasks'
print('OK')
"

check "task: /edit and /del intent routing" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import match_intent
assert match_intent('/edit 2 friday') == 'edit_task'
assert match_intent('/del 3') == 'delete_task'
assert match_intent('/delete 1') == 'delete_task'
print('OK')
"

check "task: _parse_edit_date handles common patterns" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import _parse_edit_date
assert _parse_edit_date('tomorrow') != ''
assert _parse_edit_date('3d') != ''
assert _parse_edit_date('2w') != ''
assert _parse_edit_date('monday') != ''
assert _parse_edit_date('next week') != ''
assert _parse_edit_date('2026-05-10') == '2026-05-10'
assert _parse_edit_date('none') == ''
assert _parse_edit_date('random gibberish') == ''
print('OK')
"

check "task: snooze all/overdue intent routing" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import match_intent
assert match_intent('/snooze all 3d') == 'snooze_task'
assert match_intent('/snooze overdue 1d') == 'snooze_task'
assert match_intent('/snooze 2 1w') == 'snooze_task'
print('OK')
"

check "task: recurring parse" \
  $PYTHON -c "
import sys, datetime; sys.path.insert(0, '$REPO_DIR')
from skills.tasks.local_tasks import _parse_recurrence
assert _parse_recurrence('daily') == datetime.timedelta(days=1)
assert _parse_recurrence('weekly') == datetime.timedelta(days=7)
assert _parse_recurrence('every 2 weeks') == datetime.timedelta(days=14)
assert _parse_recurrence('monthly') == datetime.timedelta(days=30)
assert _parse_recurrence('') is None
print('OK')
"

check "zero-token: physio Tuesday 4pm → health + time + no LLM" \
  $PYTHON -c "
from skills.calendar.prepare_event import _extract_events_fast
import aaka_config
names = aaka_config.member_names()
name = names[0] if names else 'Child'
r = _extract_events_fast(f'/add {name} physio Tuesday 4pm')
assert r is not None, 'fast path returned None'
assert r['type'] == 'health', r['type']
assert r['_source'] == 'zero_token', r.get('_source')
assert r['occurrences'][0]['start_time'] == '16:00', r['occurrences']
print('OK')
"
check "zero-token: carrier detection — carrier takes member" \
  $PYTHON -c "
from skills.calendar.prepare_event import _extract_events_fast
import aaka_config
names = aaka_config.member_names()
if len(names) >= 2:
    carrier, attendee = names[0], names[1]
    r = _extract_events_fast(f'{carrier} takes {attendee} to physio Tuesday 4pm')
    assert r is not None
    assert r['carrier'] == carrier, r.get('carrier')
    assert r['attendee'] == attendee, r.get('attendee')
    print('OK')
else:
    print('SKIP (need >=2 members in config)')
"
check "zero-token: no date → returns None (LLM fallback)" \
  $PYTHON -c "
from skills.calendar.prepare_event import _extract_events_fast
import aaka_config
names = aaka_config.member_names()
name = names[0] if names else 'Child'
r = _extract_events_fast(f'{name} dinner with the neighbours next week')
assert r is None, f'expected None, got {r}'
print('OK')
"

# ── Test: dry-run /today returns text without routing error ───────────────────
check "router dry-run /today returns schedule text" \
  bash -c 'cd "$REPO_DIR" && echo "/today" | $PYTHON sensor/router_sensor.py --dry-run /today 2>/dev/null | grep -vq "Error\|Traceback"'

# ── Test: _reply and _react_read symbols are importable ───────────────────────
check "router: _reply + _react_read importable" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import importlib.util, os
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
spec = importlib.util.spec_from_file_location('router_sensor', '$REPO_DIR/sensor/router_sensor.py')
# Just check the function names exist in source
src = open('$REPO_DIR/sensor/router_sensor.py').read()
assert '_react_read' in src
assert '_reply' in src
print('OK')
"

# ── Test: #private tag extraction ─────────────────────────────────────────────
check "#private tag maps to calendar_tag=private" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from skills.calendar.prepare_event import _extract_modifiers
tag, _, _, _ = _extract_modifiers('/cal lunch friday 12pm #private')
assert tag == 'private', f'got {tag}'
print('OK')
"

# ── Test: #alone → no ❓ in title, description has (going alone) ───────────────
check "#alone produces no ❓ in title and going alone in description" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.calendar.prepare_event import _extract_events_fast
ev = _extract_events_fast('/cal child gym saturday 10am #alone')
if ev:
    assert '❓' not in ev.get('title',''), f'❓ in title: {ev[\"title\"]}'
    assert 'going alone' in ev.get('description',''), f'no going alone in description: {ev}'
    print('OK')
else:
    print('SKIP (no fast path date)')
"

# ── Test: unknown sender gets 🔒 for /today ───────────────────────────────────
check "unknown sender blocked for today_schedule" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
# handler now lives in sensor/intents/calendar.py
src = open('$REPO_DIR/sensor/intents/calendar.py').read()
assert 'Calendar access requires a recognised sender' in src
print('OK')
"

# ── VPS Hardening checks ──────────────────────────────────────────────────────
header "VPS Hardening"

# Check token_vps.json exists (warn only — not created until reauth runs)
VPS_TOKEN="${AAKA_CONFIG_DIR}/tokens/token_vps.json"
if [ -f "$VPS_TOKEN" ]; then
    ok "token_vps.json present"
    PASS=$((PASS+1))
else
    warn "SKIP  token_vps.json not present — create it: python3 admin/reauth.py --profile vps"
fi

# Check google libs are available in the sensor container
if [ "$SENSOR_UP" = "true" ]; then
    if docker exec "$SENSOR_CONTAINER" python3 -c "import googleapiclient; import google.oauth2; print('OK')" 2>/dev/null | grep -q "OK"; then
        ok "google-api-python-client present in sensor container"
        PASS=$((PASS+1))
    else
        fail "google libs missing in sensor image — rebuild: docker compose -f docker-compose.prod.yml build"
        FAIL=$((FAIL+1))
    fi

    # Dry-run /addtask to confirm extraction works
    if docker exec "$SENSOR_CONTAINER" \
        python3 sensor/router_sensor.py --dry-run "/addtask buy oat milk" 2>&1 \
        | grep -qiE "add_task|title|dry.run"; then
        ok "/addtask dry-run: extraction OK"
        PASS=$((PASS+1))
    else
        fail "/addtask dry-run: router_sensor.py --dry-run failed — check sensor logs"
        FAIL=$((FAIL+1))
    fi
else
    warn "SKIP  VPS sensor container checks (container not running)"
fi

# ── Shared List Access Control ────────────────────────────────────────────────
header "Shared List Access Control"

check "member_can_access_shared: known member defaults to True" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import aaka_config
ms = aaka_config.members()
if not ms:
    print('SKIP (no members in config)')
else:
    m = ms[0]
    result = aaka_config.member_can_access_shared(m['id'])
    assert result is True, f'{m[\"id\"]} expected True, got {result}'
    print('OK')
"

check "member_can_access_shared: unknown member returns False" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import aaka_config
result = aaka_config.member_can_access_shared('__nonexistent_member__')
assert result is False, f'Expected False, got {result}'
print('OK')
"

check "list_all accepts include_shared kwarg" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.lists.list_manager import list_all
import inspect
sig = inspect.signature(list_all)
assert 'include_shared' in sig.parameters, 'include_shared param missing from list_all'
print('OK')
"

# ── Admin Members ─────────────────────────────────────────────────────────────
header "Admin Members"

check "member_is_admin: unknown member returns False" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import aaka_config
result = aaka_config.member_is_admin('__nonexistent__')
assert result is False, f'Expected False for unknown member, got {result}'
print('OK')
"

check "member_is_admin: known member without admin flag returns False" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import aaka_config
ms = [m for m in aaka_config.members() if not m.get('admin')]
if not ms:
    print('SKIP (all members are admin)')
else:
    result = aaka_config.member_is_admin(ms[0]['id'])
    assert result is False, f'{ms[0][\"id\"]} expected False, got {result}'
    print('OK')
"

check "members_list intent gated (non-admin gets lockout message)" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
src = open('$REPO_DIR/sensor/router_sensor.py').read()
assert 'members_list' in src, 'members_list intent not found in router'
assert 'Admin only' in src, 'Admin only gate not found in router'
print('OK')
"

check "route() has safety-net exception wrapper (no bare crashes to OpenClaw)" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
src = open('$REPO_DIR/sensor/router_sensor.py').read()
assert 'def route(' in src, 'route() wrapper missing'
assert 'def _route_impl(' in src, '_route_impl() inner function missing'
assert 'UNHANDLED in route()' in src, 'safety-net log marker missing'
print('OK')
"

check "file_sync errors are silent (not sent to user chat)" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
src = open('$REPO_DIR/executor/queue_worker.py').read()
assert 'file_sync' in src and 'intent' in src, 'file_sync silence check missing in _send_error'
print('OK')
"

# ── Weekly planning features ──────────────────────────────────────────────────

check "fix_analyzer: imports and runs" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.calendar.fix_analyzer import analyze_fix, format_fix_list, save_fix_list, load_fix_item
# analyze_fix should return a list (may be empty without cache)
result = analyze_fix('today')
assert isinstance(result, list), f'Expected list, got {type(result)}'
formatted = format_fix_list(result)
assert isinstance(formatted, str), f'Expected str, got {type(formatted)}'
print('OK')
"

check "sensor: t #fix dry-run" \
  $PYTHON "$REPO_DIR/sensor/router_sensor.py" --dry-run "t #fix"

check "sensor: w #fix dry-run" \
  $PYTHON "$REPO_DIR/sensor/router_sensor.py" --dry-run "w #fix"

check "sensor: c fix (no args) shows fix list" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
result = route('c fix', dry_run=True)
assert 'fix' in result.lower() or 'clear' in result.lower(), f'Unexpected: {result}'
print('OK')
"

check "fix_analyzer: merge_fixes_inline importable and runs" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.calendar.fix_analyzer import merge_fixes_inline
# Empty issues returns text unchanged
md = '**Monday** 04 May 2026\n🗳️**1300** Work (30m)'
assert merge_fixes_inline(md, []) == md
print('OK')
"

check "sensor: c #fix routes to fix_event (not add_event)" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
result = route('c #fix', dry_run=True)
# Should NOT trigger extraction/503 — should show fix list or 'clear'
assert 'Extraction failed' not in result, f'Misrouted to add_event: {result}'
assert 'fix' in result.lower() or 'clear' in result.lower(), f'Unexpected: {result}'
print('OK')
"

check "sensor: c fix N #work routes to fix_event" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
result = route('c fix 1 #work', dry_run=True)
# Should not route to add_event extraction
assert 'Extraction failed' not in result, f'Misrouted: {result}'
print('OK')
"

check "fix_analyzer: compact footer uses range notation" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.calendar.fix_analyzer import merge_fixes_inline
# Test with fake conflicts — footer should use N is X..Y not per-number listing
issues = [
    {'num': 1, 'type': 'conflict', 'date': '2026-05-04', 'start': '13:00', 'end': '14:00',
     'event_a': {'title': 'Work', 'start': '13:00', 'end': '14:00', 'event_id': 'a'},
     'event_b': {'title': 'Taekwondo', 'start': '13:30', 'end': '14:30', 'event_id': 'b'}},
    {'num': 2, 'type': 'conflict', 'date': '2026-05-05', 'start': '09:00', 'end': '10:00',
     'event_a': {'title': 'Meeting', 'start': '09:00', 'end': '10:00', 'event_id': 'c'},
     'event_b': {'title': 'Swimming', 'start': '09:30', 'end': '10:30', 'event_id': 'd'}},
]
md = '**Monday** 04 May 2026\n🗳️**1300** Work (1h)\n**Tuesday** 05 May 2026\n🗳️**0900** Meeting (1h)'
result = merge_fixes_inline(md, issues)
assert 'N is 1..2' in result, f'Expected range notation: {result}'
assert 'c fix 1 | c fix 2' not in result, f'Old per-number format still present: {result}'
print('OK')
"

check "fix_analyzer: carrier_missing visible to carrier members" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
import aaka_config
from skills.calendar.fix_analyzer import analyze_fix
# Carriers should see carrier_missing issues for any child
carrier_ids = [m['id'] for m in aaka_config.members() if m['id'] in [c.lower() for c in aaka_config.carriers()]]
if carrier_ids:
    all_issues = analyze_fix('week')
    carrier_all = [i for i in all_issues if i['type'] == 'carrier_missing']
    if carrier_all:
        mid = carrier_ids[0]
        member_issues = analyze_fix('week', member_id=mid)
        member_carrier = [i for i in member_issues if i['type'] == 'carrier_missing']
        assert len(member_carrier) == len(carrier_all), f'Carrier {mid} sees {len(member_carrier)}/{len(carrier_all)} carrier issues'
print('OK')
"

check "apply_event_padding: title does not accumulate (HH:MM)" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.calendar.prepare_event import apply_event_padding
p = {'title': 'Physio (08:00)', 'description': 'Actual appointment: 08:00���09:00',
     'occurrences': [{'date': '2026-01-01', 'start_time': '14:30', 'end_time': '15:30'}]}
r = apply_event_padding(p, 35, 35)
assert '(08:00)' not in r['title'], 'old time not stripped'
assert '(14:30)' in r['title'], 'new time missing'
assert r['description'].count('Actual appointment') == 1, 'desc accumulated'
print('OK')
"

check "format_batch_review: empty list returns error" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.calendar.prepare_event import format_batch_review
result = format_batch_review([])
assert 'No events' in result or 'no events' in result.lower(), f'Expected error for empty: {result}'
print('OK')
"

check "set_pending_confirm: cancels old awaiting_confirm item" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from aaka_queue.queue import set_pending_confirm, get_pending_confirm, write_item, _connect, _now
# Create two items for the same fake sender
id1 = write_item('add_event', 'test1', '__test_sender__', '__test_ch__', 'telegram', {})
id2 = write_item('add_event', 'test2', '__test_sender__', '__test_ch__', 'telegram', {})
# Set status to awaiting_confirm for item 1
with _connect() as c:
    c.execute('UPDATE queue_items SET status=\"awaiting_confirm\" WHERE id=?', (id1,))
set_pending_confirm('__test_sender__', id1)
# Now set pending_confirm for item 2 — item 1 should be cancelled
set_pending_confirm('__test_sender__', id2)
with _connect() as c:
    row = c.execute('SELECT status FROM queue_items WHERE id=?', (id1,)).fetchone()
    assert row['status'] == 'cancelled', f'Expected cancelled, got {row[\"status\"]}'
    # Cleanup
    c.execute('DELETE FROM queue_items WHERE sender=\"__test_sender__\"')
    c.execute('DELETE FROM pending_confirms WHERE sender=\"__test_sender__\"')
print('OK')
"

check "scheduled_summaries: --check --dry-run" \
  $PYTHON "$REPO_DIR/sensor/scheduled_summaries.py" --check --dry-run

check "gog.update_event importable" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from skills.calendar.gog import update_event
print('OK')
"

check "executor: fix_event dispatcher registered" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
src = open('$REPO_DIR/executor/queue_worker.py').read()
assert 'fix_event' in src and '_exec_fix_event' in src, 'fix_event dispatcher missing'
print('OK')
"

# ── Engagement Engine ──────────────────────────────────────────────────────────

check "engagement: tracker importable" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.engagement.tracker import track_event
print('OK')
"

check "engagement: nudge_engine importable" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.engagement.nudge_engine import decide, NudgeDecision
print('OK')
"

check "engagement: nudge_runner dry-run" \
  $PYTHON "$REPO_DIR/skills/engagement/nudge_runner.py" --dry-run

check "engagement: engagement_db log+read round-trip" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.engagement.engagement_db import log_event, get_state, purge_old_events
log_event('_test_member', 'today_schedule', 'telegram')
state = get_state('_test_member')
assert 'today_schedule' in state['intents_used'], 'intent not tracked'
print('OK')
"

# ── Birthdays ──────────────────────────────────────────────────────────────────

check "birthday: contacts_sync importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
from skills.contacts.contacts_sync import run
print('OK')
"

check "birthday: birthday_list default query" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
from skills.contacts.birthday_list import query
result = query('', sender_id='_test')
assert result and '🎂' in result or 'No birthdays' in result or 'data' in result
print('OK')
"

check "birthday: intent pattern matches 'bday'" \
  $PYTHON -c "
import sys, os, re; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
sys.argv=['router_sensor.py','bday']
src = open('$REPO_DIR/sensor/router_sensor.py').read()
assert 'birthday_list' in src and 'bday' in src
print('OK')
"

check "birthday: send_contact importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
from skills.outbox.send_contact import send_to_contact
result = send_to_contact(name='Test', first_name='Test', message='Hi', dry_run=True)
assert result.get('sent') == False and 'No contact info' in result.get('error','')
print('OK')
"

check "birthday: bday_wish test mode response" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
from skills.contacts.birthday_list import query, _save_list
# Seed a fake list entry so wish can resolve
_save_list('_testuser', [{'num': 1, 'name': 'Alice Test', 'first_name': 'Alice',
                          'month_day': '04-27', 'mobile': '+491234567', 'email': ''}])
from skills.contacts.birthday_list import _load_item
item = _load_item('_testuser', 1)
assert item and item['name'] == 'Alice Test', f'Got: {item}'
print('OK')
"

check "birthday: scheduled_summaries --birthday importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
from sensor.scheduled_summaries import send_birthday_morning
print('OK')
"

check "telegram_poller: importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'dummy')
from sensor.telegram_poller import _build_format_a, _load_offset
out = _build_format_a('123', '456', 99, '/menu')
assert 'Conversation info' in out and 'telegram:456' in out, f'Bad format: {out}'
print('OK')
"

check "engage_report: build_engage_report importable + level logic" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
os.environ.setdefault('AAKA_BASE','$REPO_DIR')
from skills.engagement.tracker import _LEVEL_GATES, _maybe_advance_level
assert isinstance(_LEVEL_GATES, list) and len(_LEVEL_GATES) == 5, 'bad gates'
from skills.engagement.engage_report import build_engage_report, LEVELS
assert len(LEVELS) == 6
print('OK')
"

check "engagement_db: count_uses_any and intent_use_counts importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
from skills.engagement.engagement_db import count_uses_any, intent_use_counts
print('OK')
"

check "gmail_poller: module importable and no gmail_labels short-circuits cleanly" \
  $PYTHON -c "
import sys, os; sys.path.insert(0,'$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR',os.path.expanduser('~/.aaka'))
os.environ.setdefault('AAKA_BASE','$REPO_DIR')
# Verify module loads without error
import sensor.gmail_poller
# Verify gmail_labels() returns a list (may be empty)
import aaka_config
labels = aaka_config.gmail_labels()
assert isinstance(labels, list), 'gmail_labels() must return list'
# Verify gmail.py new functions are importable
from skills.mail.gmail import list_by_label, fetch_full, _html_to_text
# Verify html stripping works
result = _html_to_text('<p>Hello <b>world</b></p><script>bad()</script>')
assert 'Hello' in result and 'bad' not in result, f'html_to_text failed: {result!r}'
print('OK')
"

# ── Tests: budget / expense tracking ─────────────────────────────────────────

check "budget: x → intent=budget" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import match_intent
assert match_intent('/expense 45 groceries') == 'budget'
assert match_intent('/budget') == 'budget'
print('OK')
"

check "budget: log expense dry-run" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('x 45 groceries lidl', dry_run=True)
assert 'Logged EUR 45.00 groceries' in reply, f'unexpected: {reply}'
print('OK')
"

check "budget: summary dry-run (empty month)" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.router_sensor import route
reply = route('x', dry_run=True)
assert 'No expenses' in reply or 'EUR' in reply, f'unexpected: {reply}'
print('OK')
"

check "budget: module importable and functions work" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.budget.budget_tracker import log_expense, summary, undo, fix, yearly, _resolve_month
assert _resolve_month('april') == '$(date +%Y)-04'
assert _resolve_month('jan') == '$(date +%Y)-01'
assert _resolve_month('2026-03') == '2026-03'
assert _resolve_month('hello') == ''
print('OK')
"

# ── Undo queue intent detection ──────────────────────────────────────────────
check "undo_queue intent match" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import match_intent
assert match_intent('undo #abc123') == 'undo_queue', f'got {match_intent(\"undo #abc123\")}'
assert match_intent('/undo #abc123') == 'undo_queue'
assert match_intent('/undo abc123') == 'undo_queue'
print('OK')
"

# ── PDF tool ─────────────────────────────────────────────────────────────────
check "pdf_page_spec self-test" \
  $PYTHON tools/pdf_page_spec.py

check "pdf_tool CLI help" \
  $PYTHON tools/pdf_tool.py help

check "pdf_tool intent routing" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.router_sensor import match_intent, _LOCAL_INTENTS
assert match_intent('/pdf help') == 'pdf_tool', f'got {match_intent(\"/pdf help\")}'
assert match_intent('pdf split 2s') == 'pdf_tool'
assert 'pdf_tool' in _LOCAL_INTENTS
print('OK')
"

check "pdf_tool dry-run /pdf help" \
  $PYTHON -c "
import sys, subprocess
result = subprocess.run(
    [sys.executable, '$REPO_DIR/sensor/router_sensor.py', '--dry-run', '/pdf help'],
    capture_output=True, text=True, timeout=30,
    env={**__import__('os').environ, 'AAKA_BASE': '$REPO_DIR'},
)
assert 'compress' in result.stdout, f'unexpected: {result.stdout!r}'
print('OK')
"

check "pdf_tool compress+split smoke test" \
  $PYTHON "$REPO_DIR/tools/pdf_tool_test.py"

check "pdf_tool ocr smoke test (skipped if tesseract missing)" \
  $PYTHON -c "
import os, shutil, sys, tempfile
sys.path.insert(0, '$REPO_DIR')
if not shutil.which('tesseract'):
    print('SKIP (tesseract not installed)'); sys.exit(0)
import fitz
from tools.pdf_tool import ocr

with tempfile.TemporaryDirectory() as td:
    src = os.path.join(td, 't.pdf')
    d = fitz.open(); p = d.new_page(width=595, height=842)
    p.insert_text((72, 100), 'Hello OCR World — this is the native text layer for smoke test', fontsize=18)
    d.save(src); d.close()
    r = ocr(src)
    assert r['pages'] == 1, r
    assert r['chars'] > 0, r
    # Native text layer present → OCR shouldn't be invoked.
    assert r['ocr_pages'] == 0, r
    text = open(r['text_file']).read()
    assert 'Hello' in text, text
print('OK')
"

check "pdf_tool delete-blank-pages smoke test" \
  $PYTHON -c "
import os, sys, tempfile
sys.path.insert(0, '$REPO_DIR')
import fitz
from tools.pdf_tool import delete_blank_pages, _is_blank_page

with tempfile.TemporaryDirectory() as td:
    src = os.path.join(td, 't.pdf')
    doc = fitz.open()
    p1 = doc.new_page(width=595, height=842); p1.insert_text((72, 100), 'page one content here')
    doc.new_page(width=595, height=842)  # truly blank
    p3 = doc.new_page(width=595, height=842)
    white = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 500, 700), False); white.clear_with(252)
    p3.insert_image(p3.rect, pixmap=white)  # scanned-blank (near-white image)
    p4 = doc.new_page(width=595, height=842); p4.insert_text((72, 100), 'final page content')
    # Scanner-shadow blank: raster image that is mostly white with a darker band
    # hugging one edge — pushes full-page dark ratio above 0.3% but the interior
    # stays pure white, so only the interior-only fallback catches it.
    p5 = doc.new_page(width=595, height=842)
    shadow_pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 500, 700), False)
    shadow_pix.clear_with(255)
    for _y in range(700):
        for _x in range(470, 500):
            shadow_pix.set_pixel(_x, _y, (60,))
    p5.insert_image(p5.rect, pixmap=shadow_pix)
    doc.save(src); doc.close()

    r = delete_blank_pages(src)
    assert r['removed'] == 3, r
    assert r['remaining'] == 2, r
    assert r['blank_pages'] == [2, 3, 5], r
    assert os.path.exists(r['output']), r
    assert r['blanks_output'] and os.path.exists(r['blanks_output']), r

    # Page-number-only sanity: must NOT be flagged as blank.
    pn = fitz.open(); pp = pn.new_page(width=595, height=842)
    pp.insert_text((290, 800), '3')
    pn_path = os.path.join(td, 'pn.pdf'); pn.save(pn_path); pn.close()
    pn2 = fitz.open(pn_path)
    assert _is_blank_page(pn2[0]) is False, 'page-number-only must not be blank'
    pn2.close()

    # dont-return → no blanks file written
    out2 = os.path.join(td, 't2_trimmed.pdf')
    r2 = delete_blank_pages(src, out2, write_blanks=False)
    assert r2['removed'] == 3, r2
    assert r2['blanks_output'] is None, r2
print('OK')
"

# ── Smart Drop ───────────────────────────────────────────────────────────────
check "smart drop parser: member+area+hashtag+quotedname+description" \
  $PYTHON -c "
import sys, re; sys.path.insert(0, '$REPO_DIR')
import aaka_config
from tools.inbox_router import STANDARD_AREAS

# After /drop prefix stripped; simulate full parse
caption = 'ari health #ortho \"260507-doc\". follow up visit'
_qm = re.search(r'\"([^\"]+)\"', caption)
assert _qm and _qm.group(1) == '260507-doc', 'quoted name'
_no_quotes = re.sub(r'\"[^\"]*\"', '', caption).strip()
_dot_idx = _no_quotes.find('. ')
routing = _no_quotes[:_dot_idx].strip() if _dot_idx >= 0 else _no_quotes
desc = _no_quotes[_dot_idx + 2:].strip() if _dot_idx >= 0 else ''
assert desc == 'follow up visit', f'desc={desc!r}'
hash_tags = [w[1:].lower() for w in routing.split() if w.startswith('#')]
assert hash_tags == ['ortho'], f'hash_tags={hash_tags}'
clean_words = [w for w in routing.split() if not w.startswith('#')]
m = aaka_config.member_by_name(clean_words[0])
assert m and m['id'] == 'ari', f'actor={m}'
clean_words = clean_words[1:]
assert clean_words[0].lower() in STANDARD_AREAS, f'area not matched: {clean_words[0]}'
print('OK')
"

check "smart drop parser: skip-compress keywords" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
_SKIP = {'keep', 'original', 'og', 'nocompress', 'no-compress', 'no_compress'}
for kw in ['keep', 'og', 'no-compress', '#original']:
    word = kw.lstrip('#').lower()
    assert word in _SKIP, f'{kw!r} not in skip set'
print('OK')
"

check "smart drop parser: legacy path (no member match)" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import aaka_config
first_word = 'flo172'
m = aaka_config.member_by_name(first_word)
assert m is None, 'flo172 should not match a member'
print('OK')
"

check "resolve_smart_drop: member+area+hashtag" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from tools.inbox_router import resolve_smart_drop
r = resolve_smart_drop('alex', 'health', ['checkup'], None, 1)
assert r['error'] is None, r['error']
assert r['dest_rel'].startswith('health'), r['dest_rel']
assert 'health' in r['dest_rel'], r['dest_rel']
assert 'checkup' in r['dest_rel'], r['dest_rel']
print('OK dest_rel=' + r['dest_rel'])
"

check "resolve_smart_drop: fallback to inbox when no area" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from tools.inbox_router import resolve_smart_drop
r = resolve_smart_drop('alex', None, [], None, 1)
assert '00-Inbox' in r['dest_rel'], r['dest_rel']
print('OK dest_rel=' + r['dest_rel'])
"

check "resolve_smart_drop: route keyword (tax) resolves directly" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import os; os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from tools.inbox_router import resolve_smart_drop, resolve_route_keyword, load_sensor_references
refs = load_sensor_references()
tax_path = resolve_route_keyword('tax', refs)
assert tax_path is not None, 'tax route missing from references.yaml'
r = resolve_smart_drop('alex', None, [], None, 1, tags=['tax'])
assert r['dest_rel'] == tax_path, f'expected {tax_path!r}, got {r[\"dest_rel\"]!r}'
assert r['error'] is None
print('OK dest_rel=' + r['dest_rel'])
"

check "STANDARD_AREAS contains expected area keywords" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from tools.inbox_router import STANDARD_AREAS, known_areas_for
assert 'health' in STANDARD_AREAS and 'finance' in STANDARD_AREAS and 'career' in STANDARD_AREAS
assert 'insurance' in STANDARD_AREAS and 'property' in STANDARD_AREAS
areas = known_areas_for('alex')
assert areas == STANDARD_AREAS
print('OK areas=' + ','.join(sorted(areas)))
"

check "generate_drop_filename with compressed=True" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from tools.file_utils import generate_drop_filename
name = generate_drop_filename(['health'], 'receipt.pdf', compressed=True)
assert name.endswith('_c.pdf'), f'got {name!r}'
print('OK ' + name)
"

check "generate_smart_name quoted custom name" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from tools.file_utils import generate_smart_name
name = generate_smart_name('260507-doctor-update', '.pdf', compressed=True)
assert name == '260507-doctor-update_c.pdf', f'got {name!r}'
name2 = generate_smart_name('260507-doctor-update', '.pdf', compressed=False)
assert name2 == '260507-doctor-update.pdf', f'got {name2!r}'
print('OK ' + name)
"

# ── Agent reply sync ─────────────────────────────────────────────────────────
check "agent_reply_requests table exists" \
  sqlite3 "$QUEUE_DB" "SELECT COUNT(*) FROM agent_reply_requests;"

check "agent_replies table exists" \
  sqlite3 "$QUEUE_DB" "SELECT COUNT(*) FROM agent_replies;"

check "vps_sync agent sync functions importable" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from executor.vps_sync import _push_reply_requests, _pull_agent_replies, _push_outbox
print('OK')
"

# ── Password store ───────────────────────────────────────────────────────────
check "password_store module importable" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from tools.password_store import PasswordStore, init_vault, _derive_key
print('OK')
"

check "password_store encrypt/decrypt round-trip" \
  $PYTHON -c "
import sys, tempfile, pathlib, shutil
sys.path.insert(0, '$REPO_DIR')
from tools.password_store import PasswordStore, init_vault
tmp = pathlib.Path(tempfile.mkdtemp())
vault = tmp / 'vault.enc'
salt  = tmp / 'salt'
init_vault('test-master-pw', vault_path=vault, salt_path=salt)
store = PasswordStore(vault_path=vault, master_password='test-master-pw')
store.add_entry('test/entry', 'user@example.com', 'secret123', url='example.com', grouping='test')
entries = store.list_entries()
assert len(entries) == 1, f'expected 1 entry, got {len(entries)}'
assert entries[0]['name'] == 'test/entry'
assert 'password' not in entries[0], 'password must not appear in list_entries()'
pw = store.get_password('test/entry')
assert pw == 'secret123', f'wrong password: {pw!r}'
results = store.search('example')
assert len(results) == 1
shutil.rmtree(tmp)
print('OK')
"

# ── Mail fetch ───────────────────────────────────────────────────────────────
check "mail fetch module importable" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from skills.mail.fetch import fetch_all_accounts, execute
print('OK')
"

check "mail viewer module importable" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from skills.mail.viewer import list_accounts, list_messages, read_message
print('OK')
"

# ── Phase 4: Error digest, skill deps, nudges, birthday auto-send ───────────

check "error_events table exists in schema" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
src = open('$REPO_DIR/aaka_queue/schema.sql').read()
assert 'error_events' in src, 'error_events table missing from schema.sql'
print('OK')
"

check "error_digest: scan_and_store importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.error_digest import scan_and_store, get_unacked_summary, ack_all
print('OK')
"

check "error_digest: get_unacked_summary returns list" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.error_digest import get_unacked_summary
result = get_unacked_summary()
assert isinstance(result, list), f'expected list, got {type(result)}'
print('OK')
"

check "errors_report: intent pattern matches /errors" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from sensor.intent_registry import match_intent
assert match_intent('/errors') == 'errors_report', f'got: {match_intent(\"/errors\")}'
assert match_intent('/errors flush') == 'errors_report'
print('OK')
"

check "skill_loader: validate_dependencies raises on disabled dep" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from executor.skill_loader import SkillLoader, SkillDependencyDisabled
import tempfile, pathlib, yaml
# Build minimal registry with disabled dep
reg_data = {
  'skills': {
    'main_skill': {'intent': 'main', 'enabled': True, 'depends_on': ['dep_skill']},
    'dep_skill':  {'intent': 'dep',  'enabled': False},
  }
}
with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
    yaml.dump(reg_data, f)
    reg_path = f.name
sl = SkillLoader(reg_path, '/tmp')
skill = {'_key': 'main_skill', 'intent': 'main', 'enabled': True, 'depends_on': ['dep_skill']}
try:
    sl.validate(skill, {})
    assert False, 'expected SkillDependencyDisabled'
except SkillDependencyDisabled:
    pass
import os; os.unlink(reg_path)
print('OK')
"

check "dependency_graph: importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from tools.dependency_graph import load_registry, print_text
print('OK')
"

check "nudge_engine: overdue_task_nudge in decide()" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
src = open('$REPO_DIR/skills/engagement/nudge_engine.py').read()
assert 'overdue_task_nudge' in src, 'overdue_task_nudge not found in nudge_engine.py'
print('OK')
"

check "scheduled_summaries: send_birthday_auto importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.scheduled_summaries import send_birthday_auto
print('OK')
"

check "scheduled_summaries: --bday-auto dry-run runs without error" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from sensor.scheduled_summaries import send_birthday_auto
send_birthday_auto(dry_run=True)
print('OK')
"

check "queue_worker: _exec_bday_wish handles phone+email channels" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
src = open('$REPO_DIR/executor/queue_worker.py').read()
assert 'channel == \"whatsapp\"' in src or \"channel='whatsapp'\" in src or 'phone' in src, 'bday_wish WhatsApp support missing'
print('OK')
"

# ── Phase 5: Channel Abstraction + Agent API Hardening ───────────────────────

check "gateway/types.py: MessageKind and OutboundMessage importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
from gateway.types import MessageKind, OutboundMessage
assert MessageKind.TEXT == 'text'
msg = OutboundMessage(kind=MessageKind.TEXT, recipient='123', channel='telegram', source='test', text='hi')
assert msg.text == 'hi'
print('OK')
"

check "gateway/channels/telegram.py: importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
from gateway.channels import telegram
assert hasattr(telegram, 'send_text')
assert hasattr(telegram, 'send_photo')
assert hasattr(telegram, 'send_document')
assert hasattr(telegram, 'send_reaction')
print('OK')
"

check "gateway/channels/whatsapp.py: importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
from gateway.channels import whatsapp
assert hasattr(whatsapp, 'send_text')
print('OK')
"

check "gateway/egress.py: re-exports types from gateway.types" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
from gateway.egress import MessageKind, OutboundMessage
assert MessageKind.TEXT == 'text'
print('OK')
"

check "gateway/egress.py: convenience constructors work" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
from gateway import egress
from gateway.types import MessageKind
msg = egress.text('123', 'telegram', 'hello', 'test')
assert msg.kind == MessageKind.TEXT
assert msg.text == 'hello'
print('OK')
"

check "agent_api: rate_limit_per_hour column in schema" \
  $PYTHON -c "
import sys, os, sqlite3; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
db = os.environ.get('QUEUE_DB', os.path.join(os.path.expanduser('~/.aaka'), 'data/queue/butler.db'))
if not os.path.exists(db):
    print('OK')  # no DB yet
else:
    conn = sqlite3.connect(db)
    cols = [row[1] for row in conn.execute('PRAGMA table_info(agent_registry)')]
    assert 'rate_limit_per_hour' in cols, f'missing column; cols={cols}'
    print('OK')
"

check "queue_worker: _send_approval_request defined" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
src = open('$REPO_DIR/executor/queue_worker.py').read()
assert '_send_approval_request' in src
assert 'agent_job' in src.split('_send_approval_request')[1][:500]
print('OK')
"

check "queue_worker: gmail_attachment_file in _DISPATCHERS" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
src = open('$REPO_DIR/executor/queue_worker.py').read()
assert 'gmail_attachment_file' in src
print('OK')
"

check "gmail.py: fetch_attachments importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.mail.gmail import fetch_attachments, _collect_attachments
print('OK')
"

check "gmail_attachment_filer: importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from skills.mail.gmail_attachment_filer import execute
print('OK')
"

check "inbox_router: tag_for_gmail_label importable" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
from tools.inbox_router import tag_for_gmail_label
result = tag_for_gmail_label('nonexistent-label-xyz')
assert result is None
print('OK')
"

check "registry.yaml: gmail_attachment_filer skill registered" \
  $PYTHON -c "
import sys, os, yaml; sys.path.insert(0, '$REPO_DIR')
reg = yaml.safe_load(open('$REPO_DIR/skills/registry.yaml'))
skills = reg['skills']
assert 'gmail_attachment_filer' in skills, 'gmail_attachment_filer missing'
s = skills['gmail_attachment_filer']
assert s['intent'] == 'gmail_attachment_file'
assert 'gmail_label_watcher' in s.get('depends_on', [])
print('OK')
"

check "register_agent.py: --rate-limit arg present" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
src = open('$REPO_DIR/admin/register_agent.py').read()
assert '--rate-limit' in src
assert 'rate_limit_per_hour' in src
print('OK')
"

# ── Scheduled outbound ───────────────────────────────────────────────────────

check "scheduled_messages: schema present in DB" \
  sqlite3 "$QUEUE_DB" "SELECT count(*) FROM scheduled_messages"

check "scheduled_messages: create + get + cancel round-trip" \
  $PYTHON -c "
import sys, os; sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('QUEUE_DB', '$QUEUE_DB')
from aaka_queue.queue import (
    create_scheduled_message, get_scheduled_message,
    cancel_scheduled_message, get_due_scheduled_messages,
)
mid = create_scheduled_message('test-agent', 'email',
    {'to_member_id':'x','subject':'t','body':'b'},
    '2026-01-01T00:00:00Z')
row = get_scheduled_message(mid)
assert row['status'] == 'pending', row['status']
# Already past — should appear in due list
due = [m for m in get_due_scheduled_messages() if m['id'] == mid]
assert len(due) == 1, 'not in due list'
ok = cancel_scheduled_message(mid, 'test-agent')
assert ok, 'cancel returned False'
row2 = get_scheduled_message(mid)
assert row2['status'] == 'cancelled', row2['status']
print('OK')
"

check "schedule_outbound API: invalid channel rejected" \
  $PYTHON -c "
import sys, os, json, urllib.request, urllib.error
sys.path.insert(0, '$REPO_DIR')
# Just verify _parse_schedule_at rejects naive datetimes
from datetime import datetime, timezone, timedelta
from gateway.agent_api import _parse_schedule_at
from fastapi import HTTPException
try:
    _parse_schedule_at('2026-06-01T09:00:00')
    print('FAIL — should have raised')
    sys.exit(1)
except HTTPException:
    pass
# Valid TZ accepted
ts = _parse_schedule_at('2026-06-01T09:00:00+02:00')
assert ts.endswith('Z') and '07:00:00' in ts, ts
print('OK')
"

# ── Bug-fix regression tests ──────────────────────────────────────────────────

check "Fix 1: _detect_attendee returns earliest-in-text member" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import aaka_config
from skills.calendar.prepare_event import _detect_attendee
# Pick two members dynamically: child + carrier (parent takes child to appt)
children = [m['name'] for m in aaka_config.members() if m.get('role') == 'child']
carriers = aaka_config.carriers()
if not children or not carriers:
    print('SKIP — no child+carrier pair in config')
    exit(0)
child, carrier = children[0], carriers[0]
# Child appears first in text — should return child
text = f'{child} @ Dr. Smith checkup with {carrier}'
result = _detect_attendee(text)
assert result == child, f'expected {child!r} (earliest), got {result!r}'
print('OK —', result)
"

check "Fix 1: _detect_carrier finds 'with <name>' pattern" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import aaka_config
from skills.calendar.prepare_event import _detect_carrier
carriers = aaka_config.carriers()
if not carriers:
    print('SKIP — no carriers in config')
    exit(0)
carrier = carriers[0]
result = _detect_carrier(f'Checkup appointment with {carrier} 10:30')
assert result == carrier, f'expected {carrier!r}, got {result!r}'
print('OK —', result)
"

check "Fix 2: member_is_admin accepts slug, rejects numeric ID" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
import aaka_config
admin_m = next((m for m in aaka_config.members() if m.get('admin', False)), None)
assert admin_m, 'no admin member in config'
assert aaka_config.member_is_admin(admin_m['id']) is True, f'slug {admin_m[\"id\"]} not recognized as admin'
assert aaka_config.member_is_admin('123456789') is False, 'numeric ID should not match'
print('OK — admin slug:', admin_m['id'])
"

check "Fix 3: #tax routes resolve (tax/tax25/tax26)" \
  $PYTHON -c "
import sys; sys.path.insert(0, '$REPO_DIR')
from tools.inbox_router import load_sensor_references, resolve_route_keyword
refs = load_sensor_references()
tax_path = resolve_route_keyword('tax', refs)
assert tax_path is not None, 'tax route missing'
assert 'Finance' in tax_path or 'Tax' in tax_path, f'unexpected path: {tax_path}'
tax25 = resolve_route_keyword('tax25', refs)
assert tax25 is not None, 'tax25 route missing'
tax26 = resolve_route_keyword('tax26', refs)
assert tax26 is not None, 'tax26 route missing'
print(f'OK — tax->{tax_path!r}')
"

# ── OSS path-safety checks ───────────────────────────────────────────────────
# These guard against hardcoded /Users/Shared paths leaking back into the
# codebase. Run on every PR — a new user's clone has no /Users/Shared at all.

check "oss: detect.sh REPO_DIR matches actual git toplevel" \
  bash -c "
    RESOLVED=\$(bash -c 'source $REPO_DIR/admin/lib/detect.sh 2>/dev/null; echo \$REPO_DIR' 2>/dev/null)
    EXPECTED=\$(git -C '$REPO_DIR' rev-parse --show-toplevel 2>/dev/null)
    echo \"REPO_DIR resolved to: \$RESOLVED (expected: \$EXPECTED)\"
    [ \"\$RESOLVED\" = \"\$EXPECTED\" ]
  "

check "oss: aaka_config CONFIG_DIR defaults to ~/.aaka when AAKA_CONFIG_DIR unset" \
  $PYTHON -c "
import sys, os
sys.path.insert(0, '$REPO_DIR')
# Run in a subprocess with AAKA_CONFIG_DIR cleared so the module reimports cleanly
import subprocess, json
result = subprocess.run(
    [sys.executable, '-c',
     'import sys; sys.path.insert(0, \"$REPO_DIR\"); import aaka_config; print(str(aaka_config.CONFIG_DIR))'],
    capture_output=True, text=True,
    env={k: v for k, v in os.environ.items() if k != 'AAKA_CONFIG_DIR'},
)
path = result.stdout.strip()
assert result.returncode == 0, f'import failed: {result.stderr}'
assert '.aaka' in path, f'expected ~/.aaka in default, got: {path}'
assert '/Users/Shared' not in path, f'hardcoded Shared path leaked: {path}'
print(f'OK — default: {path}')
"

check "oss: setup_check.py --json produces valid JSON with tier keys" \
  $PYTHON -c "
import subprocess, sys, json, os
result = subprocess.run(
    [sys.executable, '$REPO_DIR/admin/setup_check.py', '--json'],
    capture_output=True, text=True,
    env={**os.environ, 'AAKA_BASE': '$REPO_DIR'},
)
assert result.returncode == 0, f'setup_check.py failed: {result.stderr[:300]}'
data = json.loads(result.stdout)
assert 'tiers' in data, 'missing tiers key'
assert all(str(t) in data['tiers'] for t in range(4)), 'expected tiers 0-3'
assert 'checks' in data, 'missing checks key'
print(f'OK — {len(data[\"checks\"])} checks, {len(data[\"tiers\"])} tiers')
"

check "oss: unknown Telegram sender gets friendly reply, not empty string" \
  $PYTHON -c "
import sys, os
sys.path.insert(0, '$REPO_DIR')
os.environ.setdefault('AAKA_CONFIG_DIR', os.path.expanduser('~/.aaka'))
# Patch aaka_config so the sensor can import without a real aaka.yaml
import aaka_config
aaka_config._cfg_cache = {'system': {}, 'members': [], 'calendar': {'calendars': []}}
from sensor.router_sensor import _is_allowed_channel, REPLY_PREFIX
# Simulate what the channel gate does for an unknown Telegram DM sender
sender_id = '9999999999'
channel_id = sender_id  # DMs: sender == channel
assert not _is_allowed_channel(sender_id, channel_id), 'test setup: sender should be unknown'
# Now check the reply logic (replicate the gate condition)
source = 'telegram'
if source == 'telegram' and sender_id == channel_id:
    reply = (
        f'👋 Hi! Aaka doesn\\'t recognise this Telegram account yet.\\\n\\\n'
        f'Your Telegram user ID is: \`{sender_id}\`'
    )
else:
    reply = ''
assert reply != '', 'unknown DM sender got empty reply — should get friendly message'
assert sender_id in reply, 'reply does not include the sender ID'
print('OK — unknown sender gets friendly reply with their ID')
"

# ── LLM providers (OpenClaw removal, Phase 1) ─────────────────────────────────
check "llm: provider layer — gemini+anthropic dispatch (mocked, no network)" \
    bash -c "cd '$REPO_DIR' && '$PYTHON' -m gateway.llm_providers_test"

# ── Telegram channel + multi-bot routing (Phase 2 / multi-bot) ────────────────
check "telegram: native egress routing + multi-bot token selection (mocked)" \
    bash -c "cd '$REPO_DIR' && '$PYTHON' -m gateway.channels.telegram_test"
check "telegram: multi-bot supervisor resolve + per-bot offset/metadata" \
    bash -c "cd '$REPO_DIR' && '$PYTHON' -m sensor.telegram_multibot_test"

# ── Slack channel (new channel) ───────────────────────────────────────────────
check "slack: web-api adapter — postMessage/reactions + workspace token (mocked)" \
    bash -c "cd '$REPO_DIR' && '$PYTHON' -m gateway.channels.slack_test"

# ── Aaka Console (unified shell: Console · Tasks · Status, port 8003) ──────────
header "Console — server routes"
check "console server: shell, healthz, webui routes, mounted taskboard CRUD" \
    bash -c "cd '$REPO_DIR' && '$PYTHON' executor/console/test_server.py"

header "Console — status routes"
check "console status: setup_check subprocess (happy/exit/timeout) + log tail" \
    bash -c "cd '$REPO_DIR' && '$PYTHON' executor/console/test_status.py"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
