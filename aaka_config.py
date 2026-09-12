"""
Aaka — context config loader.

Reads aaka.yaml from the external config directory.
Single source of truth for member data, calendar IDs, and timezone.

Environment variables:
  AAKA_CONTEXT     semantic context label (default: family) — does NOT affect paths
  AAKA_CONFIG_DIR  root of external config (default: ~/.aaka)
  AAKA_BASE        repo root override (default: parent of this file)
  FAMILY_BUTLER_BASE  legacy alias for AAKA_BASE (backwards compat)
"""

import datetime
import json
import os
import re
import yaml
from pathlib import Path
from functools import lru_cache

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE = Path(
    os.environ.get("AAKA_BASE")
    or os.environ.get("FAMILY_BUTLER_BASE")
    or Path(__file__).parent
)

CONTEXT = os.environ.get("AAKA_CONTEXT", "family")

CONFIG_DIR = Path(os.environ.get("AAKA_CONFIG_DIR") or Path.home() / ".aaka")
TOKENS_DIR = CONFIG_DIR / "tokens"


def _load_env_file(path: Path) -> None:
    # Source KEY=VALUE pairs from path into os.environ without overriding
    # existing values. Quiet on missing/malformed lines — launchd processes
    # only get explicit EnvironmentVariables, so this is how runtime secrets
    # (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, etc.) reach the process.
    if not path.exists():
        return
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


_load_env_file(CONFIG_DIR / ".env")


def _load_system_yaml():
    """Load system.yaml if present; return dict or {}."""
    system_yaml = CONFIG_DIR / "config" / "system.yaml"
    if system_yaml.exists():
        with open(system_yaml) as f:
            return yaml.safe_load(f) or {}
    return {}

_system = _load_system_yaml()

VAULT_PATH = Path(
    os.environ.get("VAULT_PATH")
    or _system.get("vault_path")
    or CONFIG_DIR / "vault"
)

SHARED_VAULT_PATH = Path(
    os.environ.get("SHARED_VAULT_PATH")
    or _system.get("shared_vault_path")
    or CONFIG_DIR / "vault-shared"
)


def vault_path_for(member_id: str) -> Path:
    """Return the vault root for a given member. Falls back to global VAULT_PATH."""
    try:
        members_cfg = {m["id"]: m for m in _load().get("members", [])}
        vp = members_cfg.get(member_id, {}).get("vault_path")
        if vp:
            return Path(vp)
    except Exception:
        pass
    return VAULT_PATH

# Derived data paths
DATA_DIR      = CONFIG_DIR / "data"
CALENDAR_DIR  = DATA_DIR / "calendar"
TASKS_DIR     = DATA_DIR / "tasks"
CONTACTS_DIR  = DATA_DIR / "contacts"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
QUEUE_DIR     = DATA_DIR / "queue"
LOGS_DIR      = CONFIG_DIR / "logs"


def secrets_root() -> Path:
    """Root dir for per-project secrets (AAKA_SECRETS_ROOT, default ~/.aaka/secrets)."""
    return Path(os.environ.get("AAKA_SECRETS_ROOT") or Path.home() / ".aaka" / "secrets")


# ── Config loader ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load() -> dict:
    cfg_file = CONFIG_DIR / "config" / "aaka.yaml"
    return yaml.safe_load(cfg_file.read_text())



# ── Public API (unchanged from family_config.py) ───────────────────────────

def members() -> list[dict]:
    # aaka.yaml members + dynamic members created via onboarding (no yaml edits).
    base = _load().get("members") or []
    dyn = _dynamic_members()
    if not dyn:
        return base
    seen = {m.get("id") for m in base}
    return base + [m for m in dyn if m.get("id") not in seen]


def _dynamic_members_path() -> Path:
    return CONFIG_DIR / "data" / "members_dynamic.json"


def _dynamic_members() -> list[dict]:
    import json as _json
    p = _dynamic_members_path()
    try:
        return _json.loads(p.read_text()) if p.exists() else []
    except Exception:
        return []


def add_dynamic_member(name: str) -> dict:
    """Create a minimal member (id derived from name) in data/members_dynamic.json
    and return it. Lets onboarding add people without ever editing aaka.yaml."""
    import json as _json
    import re as _re
    existing = {m.get("id") for m in members()}
    base_id = _re.sub(r"[^a-z0-9]", "", (name or "").lower()) or "member"
    mid, n = base_id, 2
    while mid in existing:
        mid = f"{base_id}{n}"
        n += 1
    m = {"id": mid, "name": (name or mid).strip(), "role": "member",
         "admin": False, "namespace": mid, "source": "invite"}
    dyn = _dynamic_members()
    dyn.append(m)
    _save_dynamic_members(dyn)
    return m


def _save_dynamic_members(dyn: list) -> None:
    import json as _json
    p = _dynamic_members_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(dyn, indent=2))
    os.replace(tmp, p)


def remove_dynamic_member(member_id: str) -> bool:
    """Remove a dynamically-created member. Returns True if one was removed.
    Never touches aaka.yaml members (only the dynamic store)."""
    dyn = _dynamic_members()
    kept = [m for m in dyn if m.get("id") != member_id]
    if len(kept) == len(dyn):
        return False
    _save_dynamic_members(kept)
    return True


# ── Groups (purpose-bound chats) ─────────────────────────────────────────────
def groups() -> dict:
    """Return the groups: section of aaka.yaml, or {} if absent."""
    return _load().get("groups") or {}


def group_for(channel_id: str) -> dict | None:
    """Return the group def whose chat_ids includes this channel_id, else None.

    Accepts either bare channel_ids ("-1001234567890", "web:notes") or
    prefixed forms ("telegram:-1001234567890"). The returned dict includes
    an "id" key set to the group's name for convenience.
    """
    if not channel_id:
        return None
    cid = str(channel_id)
    for group_id, gdef in groups().items():
        chat_ids = gdef.get("chat_ids") or []
        for entry in chat_ids:
            if entry == cid:
                return {**gdef, "id": group_id}
            # Accept "telegram:-100..." matching "-100..." (and vice versa)
            if ":" in entry and entry.split(":", 1)[1] == cid:
                return {**gdef, "id": group_id}
            if ":" in cid and cid.split(":", 1)[1] == entry:
                return {**gdef, "id": group_id}
    return None


def notifications_group() -> dict | None:
    """Return the first group with purpose='notifications' (if any)."""
    for gid, gdef in groups().items():
        if gdef.get("purpose") == "notifications":
            return {**gdef, "id": gid}
    return None


def notifications_chat_id() -> str:
    """Return the first chat_id of the notifications group, or '' if none."""
    g = notifications_group()
    if not g:
        return ""
    chat_ids = g.get("chat_ids") or []
    return chat_ids[0] if chat_ids else ""


def default_actor() -> str:
    """Return the member id used as a fallback actor for tasks/notes/vault paths.

    Resolution: first member with admin: true, else first member, else "default".
    Used in places that historically hardcoded a specific personal member id.
    """
    ms = members()
    admin = next((m for m in ms if m.get("admin")), None)
    if admin:
        return admin["id"]
    if ms:
        return ms[0]["id"]
    return "default"


def calendar_id() -> str:
    # Back-compat: old configs use a top-level `calendar_id`; the current setup
    # template writes `calendar.default_id`. Accept either, default to "primary".
    cfg = _load()
    return (cfg.get("calendar_id")
            or (cfg.get("calendar") or {}).get("default_id")
            or "primary")


def timezone() -> str:
    # Back-compat: old configs use top-level `timezone`; the setup template writes
    # `system.timezone`. Accept either, default to UTC.
    cfg = _load()
    return (cfg.get("timezone")
            or (cfg.get("system") or {}).get("timezone")
            or "UTC")


_BOT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{1,15}$")


def bot_name() -> str:
    """Display name of the bot (e.g. 'Aaka'). Configurable via bot_name in aaka.yaml.

    Forking the project to rename the bot should be a one-line change in
    aaka.yaml. The default 'Aaka' is used when the key is absent.

    Validated against [a-zA-Z][a-zA-Z0-9_-]{1,15} so it is safe to use in
    file paths, log namespaces, and intent regex prefixes.
    """
    cfg = _load()
    # Accept top-level (old) or system.bot_name (setup template); default Aaka.
    name = cfg.get("bot_name") or (cfg.get("system") or {}).get("bot_name") or "Aaka"
    if not _BOT_NAME_RE.match(name):
        raise ValueError(
            f"bot_name {name!r} is invalid — must match [a-zA-Z][a-zA-Z0-9_-]{{1,15}}"
        )
    return name


def branded_name() -> str:
    """Brand mark used on marketing/welcome surfaces (e.g. '&Aaka').

    Use this on surfaces where the brand mark is appropriate: welcome
    message, signature lines, Telegram bot bio. Do NOT use it for file
    paths, log namespaces, or intent regex prefixes — use bot_name() there.
    """
    return f"&{bot_name()}"


def bot_emoji() -> str:
    cfg = _load()
    return cfg.get("bot_emoji") or (cfg.get("system") or {}).get("bot_emoji") or "🌤️"


def reply_prefix() -> str:
    return f"{bot_emoji()} {bot_name()}: "


def member_names() -> list[str]:
    return [m["name"] for m in members()]


def group_name() -> str:
    """Name of the shared/whole-household member (role: family), used as default attendee."""
    for m in members():
        if m.get("role") == "family":
            return m["name"]
    return _load().get("group_name", "Household")


def carriers() -> list[str]:
    return [m["name"] for m in members() if m.get("is_carrier")]


def needs_carrier() -> set[str]:
    return {m["name"] for m in members() if m.get("needs_carrier")}


def name_by_email(email: str) -> str:
    for m in members():
        if m.get("email", "").lower() == email.lower():
            return m["name"]
    return ""


def holiday_calendars() -> list[dict]:
    """Return list of {id, match: {description: [...], title: [...]}} holiday calendar configs."""
    return _load().get("holiday_calendars", [])


def _is_public_holiday(date_str: str) -> bool:
    """Check cached holidays.json. Fail-open: returns False if file missing or unreadable."""
    path = CALENDAR_DIR / "holidays.json"
    if not path.exists():
        return False
    try:
        return date_str in json.loads(path.read_text())
    except Exception:
        return False


def guest_emails_for(attendee: str, carrier: str = "", start_time: str = "", date: str = "") -> list[str]:
    """Return invite emails for attendee and carrier (only those with invite_as_guest=true).

    If a member has a calendars.personal.id, use that as the invite email.
    If a member has a calendars.work.id AND the event is in work hours (08:00–19:00)
    on a weekday that is not a public holiday, also add the work email.
    Falls back to flat email field for members without calendars.
    """
    in_work_hours = False
    if start_time:
        try:
            h = int(start_time.split(":")[0])
            in_work_hours = 8 <= h < 19
        except (ValueError, IndexError):
            pass
    if in_work_hours and date:
        try:
            d = datetime.date.fromisoformat(date)
            if d.weekday() >= 5:        # Sat/Sun
                in_work_hours = False
        except ValueError:
            pass
    if in_work_hours and date:
        if _is_public_holiday(date):
            in_work_hours = False

    emails = []
    for name in set(filter(None, [attendee, carrier])):
        for m in members():
            if m["name"] != name:
                continue
            if not m.get("invite_as_guest"):
                break
            cals = m.get("calendars", {})
            if cals:
                personal_id = (cals.get("personal") or {}).get("id", "")
                if personal_id:
                    emails.append(personal_id)
                if in_work_hours:
                    work_id = (cals.get("work") or {}).get("id", "")
                    if work_id:
                        emails.append(work_id)
            elif m.get("email"):
                emails.append(m["email"])
            break
    return emails


def target_calendar_id(sender_member_id: str, calendar_tag: str = "") -> str:
    """Return the Google Calendar ID to write the event to.

    Looks up the sender's calendars block; uses calendar_tag if given and valid,
    else default_calendar. Falls back to shared calendar_id() if sender has no
    calendars block or is unknown.

    Special tag handling:
      "private" → maps to "personal" calendar key
      "work"    → silently ignored (can't write to work calendars); falls back to default
    """
    import logging as _logging
    _log = _logging.getLogger("aaka_config")

    # Normalise tag aliases
    tag_norm = calendar_tag.lower() if calendar_tag else ""
    if tag_norm == "private":
        tag_norm = "personal"
    elif tag_norm == "work":
        _log.warning("target_calendar_id: 'work' tag ignored — cannot write to work calendars")
        tag_norm = ""

    for m in members():
        if m.get("id") == sender_member_id:
            cals = m.get("calendars", {})
            if not cals:
                break
            tag = tag_norm if (tag_norm and tag_norm in cals) else m.get("default_calendar", "")
            if tag and tag in cals:
                return cals[tag].get("id") or calendar_id()
            break
    return calendar_id()


# Per-channel member fields. Each channel accepts more than one spelling because
# the setup wizard, aaka.yaml.example and the older hand-written configs disagree
# (e.g. the sample writes `whatsapp_phone` while the code always read `whatsapp`).
# First non-empty wins; every listed alias is matched by member_by_sender().
CHANNEL_FIELDS: dict = {
    "telegram": ("telegram_id", "telegram"),
    "whatsapp": ("whatsapp", "whatsapp_phone"),
    "signal":   ("signal", "signal_number"),
}


def member_handle(member: dict, channel: str) -> str:
    """The handle a member is reachable at on `channel` ("" when none)."""
    for field in CHANNEL_FIELDS.get(channel, ()):
        val = member.get(field)
        if val not in (None, ""):
            return str(val).strip()
    return ""


# Order in which a scheduled push (summary, tool report, nudge) reaches a member
# who has several handles. Only channels in ENABLED_CHANNELS count — a WhatsApp
# number on the roster is not a route when the sidecar is switched off.
CHANNEL_PREFERENCE = ("telegram", "whatsapp", "signal")


def enabled_channels() -> list:
    return [c.strip() for c in os.environ.get("ENABLED_CHANNELS", "telegram").split(",")
            if c.strip()]


def preferred_handle(member: dict, enabled: "list | None" = None) -> "tuple[str, str]":
    """(handle, channel) for the first enabled channel `member` is reachable on;
    ("", "") when there is none. The channel name doubles as the outbox `source`
    so sensor/flush_outbox.py picks the matching egress adapter."""
    on = set(enabled if enabled is not None else enabled_channels())
    for channel in CHANNEL_PREFERENCE:
        if channel not in on:
            continue
        handle = member_handle(member or {}, channel)
        if handle:
            return handle, channel
    return "", ""


def member_targets(enabled: "list | None" = None) -> dict:
    """{member_id: (handle, channel)} for every roster member a push can reach."""
    out = {}
    for m in members():
        handle, channel = preferred_handle(m, enabled)
        if handle:
            out[m["id"]] = (handle, channel)
    return out


def member_by_sender(sender: str) -> dict | None:
    """Resolve E.164 phone, WhatsApp JID, Signal number/uuid, email, or Telegram
    ID → member dict. Returns None if unrecognised."""
    if not sender:
        return None
    s = sender.strip().lower()
    # Normalise WhatsApp JIDs: "4915123146203@s.whatsapp.net" → "+4915123146203"
    _wa_norm = s
    if s.endswith("@s.whatsapp.net"):
        _wa_norm = "+" + s.replace("@s.whatsapp.net", "")
    elif s.endswith("@lid"):
        _wa_norm = s  # device-linked JID — no E.164 equivalent
    for m in members():
        for channel in CHANNEL_FIELDS:
            # str() so an unquoted YAML int still matches a string ID.
            handle = str(member_handle(m, channel)).strip().lower()
            if handle and (s == handle or _wa_norm == handle):
                return m
        if s == (m.get("email") or "").strip().lower():
            return m
    # Dynamic allowlist (populated by the /invite onboarding flow) — maps a
    # WhatsApp handle (@lid or +E.164) → member id, without editing aaka.yaml.
    al = wa_allowlist()
    mid = al.get(s) or al.get(_wa_norm)
    if mid:
        return next((m for m in members() if m.get("id") == mid), None)
    return None


def _wa_allowlist_path() -> Path:
    return CONFIG_DIR / "data" / "wa_allowlist.json"


def wa_allowlist() -> dict:
    """Handle → member_id map from data/wa_allowlist.json. Lowercased keys."""
    import json as _json
    p = _wa_allowlist_path()
    try:
        if p.exists():
            return {str(k).strip().lower(): v for k, v in _json.loads(p.read_text()).items()}
    except Exception:
        pass
    return {}


def primary_calendar_id(member_name: str) -> str:
    """Return the primary calendar ID for a member, or '' if not set."""
    for m in members():
        if m["name"] == member_name:
            return m.get("primary_calendar_id", "") or ""
    return ""


def all_calendar_ids() -> list[str]:
    """Return all distinct calendar IDs across the shared calendar and all members."""
    ids: set[str] = set()
    cid = calendar_id()  # robust: top-level calendar_id OR calendar.default_id
    if cid:
        ids.add(cid)
    for m in members():
        if m.get("primary_calendar_id"):
            ids.add(m["primary_calendar_id"])
        for cid in m.get("conflict_calendars", []):
            if cid:
                ids.add(cid)
        for cal in m.get("calendars", {}).values():
            if isinstance(cal, dict) and cal.get("id"):
                ids.add(cal["id"])
    return list(ids)


def member_calendar_ids(member_id: str) -> list[str]:
    """Return all calendar IDs for a member (all calendars block entries)."""
    for m in members():
        if m.get("id") == member_id:
            return [
                cal["id"] for cal in m.get("calendars", {}).values()
                if isinstance(cal, dict) and cal.get("id")
            ]
    return []


def calendar_is_private(cal_id: str) -> bool:
    """Return True if the calendar has mark_private: true in aaka.yaml."""
    cfg = _load()
    if cal_id == cfg.get("calendar_id"):
        return bool(cfg.get("mark_private"))
    for m in members():
        for cal in m.get("calendars", {}).values():
            if isinstance(cal, dict) and cal.get("id") == cal_id:
                return bool(cal.get("mark_private"))
    return False


def calendar_private_member(cal_id: str) -> str:
    """Return member name if calendar has mark_private: true, else ''."""
    for m in members():
        for cal in m.get("calendars", {}).values():
            if isinstance(cal, dict) and cal.get("id") == cal_id and cal.get("mark_private"):
                return m["name"]
    return ""


def calendar_label(cal_id: str) -> str:
    """Reverse-map a calendar ID to a 'member/key' label for display."""
    cfg = _load()
    if cal_id == cfg.get("calendar_id"):
        return "family"
    for m in members():
        for key, cal in m.get("calendars", {}).items():
            if isinstance(cal, dict) and cal.get("id") == cal_id:
                return f"{m['id']}/{key}"
    return cal_id


def calendar_emoji_map() -> dict[str, str]:
    """Return {calendar_id: emoji} for all calendars in aaka.yaml."""
    result = {}
    for m in members():
        for cal in m.get("calendars", {}).values():
            if isinstance(cal, dict) and cal.get("id") and cal.get("emoji"):
                result[cal["id"]] = cal["emoji"]
    return result


def reserved_hours() -> list[dict]:
    """Load reserved_hours blocks from aaka.yaml.
    Each entry: {"label": str, "days": [str], "start": "HH:MM", "end": "HH:MM"}
    """
    return _load().get("reserved_hours", [])


def auth_members() -> list[dict]:
    """Return members that have an auth block."""
    return [m for m in members() if "auth" in m]


def member_is_admin(member_id: str) -> bool:
    """Return True if member has admin: true in aaka.yaml (default: False)."""
    m = next((x for x in members() if x.get("id") == member_id), None)
    return bool(m.get("admin", False)) if m else False


def member_can_access_shared(member_id: str) -> bool:
    """Return True if member is allowed to access shared lists (default: True).

    Set shared_access: false in aaka.yaml to block a member from shared lists.
    Unrecognised member IDs always return False.
    """
    m = next((x for x in members() if x.get("id") == member_id), None)
    if m is None:
        return False
    return bool(m.get("shared_access", True))


def member_by_name(name: str) -> dict | None:
    """Find member by display name or id (case-insensitive)."""
    name_lower = name.lower()
    for m in members():
        if m.get("name", "").lower() == name_lower or m.get("id", "").lower() == name_lower:
            return m
    return None


def family_group_for(member_id: str) -> list[str]:
    """Return the family_group member id list for a given member id."""
    for m in members():
        if m.get("id") == member_id:
            return m.get("family_group", [])
    return []


def resolve_guests(tags: list[str], sender_id: str, start_time: str = "") -> list[str]:
    """Resolve @fam / @all / @work / @<member> tags into invite email list.

    @fam       → family_group_for(sender_id) members with invite_as_guest: true
    @all       → all members with invite_as_guest: true
    @work      → sender's own work calendar id (CC only)
    @<member>  → that member's invite email (personal calendar id or .email).
                 Bypasses invite_as_guest (explicit request from the sender).
    Deduplicates result. Tags that don't resolve are silently skipped here —
    surfacing them as a warning happens upstream in prepare_event.
    """
    result: list[str] = []

    def _email_for_member(m: dict, *, require_invite_flag: bool = True) -> str | None:
        if require_invite_flag and not m.get("invite_as_guest"):
            return None
        cals = m.get("calendars", {})
        if cals:
            pid = (cals.get("personal") or {}).get("id", "")
            if pid:
                return pid
        return m.get("email") or None

    for tag in tags:
        if tag == "@fam":
            group_ids = family_group_for(sender_id)
            for mid in group_ids:
                for m in members():
                    if m.get("id") == mid:
                        e = _email_for_member(m)
                        if e:
                            result.append(e)
        elif tag == "@all":
            for m in members():
                e = _email_for_member(m)
                if e:
                    result.append(e)
        elif tag == "@work":
            for m in members():
                if m.get("id") == sender_id:
                    work_id = (m.get("calendars", {}).get("work") or {}).get("id", "")
                    if work_id:
                        result.append(work_id)
        else:
            # @<member> — explicit per-person invite (bypasses invite_as_guest)
            stripped = tag.lstrip("@")
            m = member_by_name(stripped)
            if m:
                e = _email_for_member(m, require_invite_flag=False)
                if e:
                    result.append(e)

    # Deduplicate preserving order
    seen: set[str] = set()
    deduped = []
    for e in result:
        if e not in seen:
            seen.add(e)
            deduped.append(e)
    return deduped


def is_known_guest_tag(tag: str) -> bool:
    """True if `@<x>` is a recognised guest tag: fam/all/work or any member id/name."""
    low = tag.lower().lstrip("@")
    if low in ("fam", "all", "work"):
        return True
    return member_by_name(low) is not None


def calendar_type(cal_id: str) -> str:
    """Return 'personal' | 'work' | 'family' for a calendar ID."""
    cfg = _load()
    if cal_id == cfg.get("calendar_id"):
        return "family"
    for m in members():
        cals = m.get("calendars", {})
        if cal_id == (cals.get("personal") or {}).get("id"):
            return "personal"
        if cal_id == (cals.get("work") or {}).get("id"):
            return "work"
    return "personal"  # fallback


# Derived cache paths
MEMBER_EVENTS_CACHE = CALENDAR_DIR / "member_events.json"


def auth_for(member_id: str) -> dict | None:
    """Return {token_file: Path, scopes: list} for a member, or None."""
    for m in members():
        if m.get("id") == member_id and "auth" in m:
            auth = m["auth"]
            return {
                "token_file": TOKENS_DIR / auth["token_file"],
                "scopes": auth["scopes"],
            }
    return None


# Canonical user-facing process labels. `away` is the stateless sensor on a
# VPS; `home` is the executor on the machine that holds OAuth tokens.
# Legacy aliases (`sensor`, `executor`) are accepted from AAKA_ROLE and from
# the `roles:` block in aaka.yaml.
_ROLE_ALIASES = {
    "sensor": "away",
    "executor": "home",
    "away": "away",
    "home": "home",
}


def role() -> str:
    """Process role: 'away' (stateless sensor on a VPS) or 'home' (executor
    on the machine that holds OAuth tokens). Default: home.

    Reads AAKA_ROLE from the environment. Legacy values ('sensor', 'executor')
    are accepted and canonicalised.
    """
    raw = os.environ.get("AAKA_ROLE", "home").strip().lower()
    return _ROLE_ALIASES.get(raw, raw)


def skill_enabled(skill_name: str) -> bool:
    """Return False if skill_name is in this role's skills_disabled list.

    Defined under `roles:` in aaka.yaml. Skills consult this at the top of
    their entry point and return {"skipped": "..."} when disabled — the
    caller treats that as a silent no-op (no log, no error).

    The role block can be keyed by the canonical name (`away`/`home`) or the
    legacy name (`sensor`/`executor`). Canonical wins on conflict.
    """
    roles_cfg = _load().get("roles", {}) or {}
    canon = role()
    legacy = {"away": "sensor", "home": "executor"}.get(canon, canon)
    role_cfg = roles_cfg.get(canon) or roles_cfg.get(legacy) or {}
    return skill_name not in (role_cfg.get("skills_disabled") or [])


def gmail_labels() -> list[dict]:
    """Return gmail_labels config block from aaka.yaml, or empty list.

    Each entry: {label, prompt, output, topic?}
    output: "telegram" | "note" | "task"
    """
    return _load().get("gmail_labels", [])


if __name__ == "__main__":
    print(f"Context : {CONTEXT}")
    print(f"CONFIG_DIR: {CONFIG_DIR}")
    print(f"TOKENS_DIR: {TOKENS_DIR}")
    print(f"VAULT_PATH: {VAULT_PATH}")
    print(f"Config file: {CONFIG_DIR / 'config' / 'aaka.yaml'}")
    print(f"Members: {member_names()}")
    print(f"Calendar: {calendar_id()}")
    print(f"Timezone: {timezone()}")
