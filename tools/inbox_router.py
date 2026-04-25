#!/usr/bin/env python3
"""
tools/inbox_router.py — Zero-LLM tag resolution and routing engine.

Tag resolution — 6-step chain:
  1. Exact entity match          → O(1), no LLM
  2. Exact action match          → O(1), no LLM
  3. Top-level alias lookup      → O(1), no LLM
  4. Per-entity alias scan       → O(n entities), no LLM
  5. difflib fuzzy match ≥0.72   → ~1ms, no LLM
  6. Unknown → log to .memory/unknown-tags.md, move to 00-Inbox/unprocessed/

Usage:
  python3 tools/inbox_router.py --tags "flo172 tax expense" --namespace alice --dry-run
"""

import argparse
import difflib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))


def _vault_path() -> Path:
    from aaka_config import vault_path_for, default_actor
    return vault_path_for(default_actor())


def _references_file() -> Path:
    """Canonical path for references.yaml — global config, not per-vault.

    Lives alongside aaka.yaml in $AAKA_CONFIG_DIR/config/ so both the sensor
    (VPS) and executor (Mac) read and write the same file without any sync.
    """
    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
    return config_dir / "config" / "references.yaml"


def _parse_refs(data: dict) -> dict:
    return {
        "entities": data.get("entities") or {},
        "actions": data.get("actions") or {},
        "aliases": data.get("aliases") or {},
        "routes": data.get("routes") or {},
        "areas": data.get("areas") or {},
    }


def load_references(vault: "Path | None" = None) -> dict:
    """Load references.yaml from $AAKA_CONFIG_DIR/config/.

    The vault parameter is kept for backwards compatibility but ignored —
    references.yaml is now global config, not per-vault.
    """
    ref_file = _references_file()
    if not ref_file.exists():
        return {"entities": {}, "actions": {}, "aliases": {}, "routes": {}}
    with open(ref_file) as f:
        data = yaml.safe_load(f) or {}
    return _parse_refs(data)


def resolve_tag(tag: str, refs: dict) -> tuple[str, str, dict]:
    """
    Resolve a single tag using the 6-step chain.

    Returns (kind, key, definition) where kind is one of:
      'entity', 'action', 'alias_entity', 'alias_action', 'fuzzy', 'unknown'
    """
    tag_lower = tag.lower()
    entities = refs["entities"]
    actions = refs["actions"]
    top_aliases = refs["aliases"]

    # Step 1: exact entity match
    if tag_lower in entities:
        return ("entity", tag_lower, entities[tag_lower])

    # Step 2: exact action match
    if tag_lower in actions:
        return ("action", tag_lower, actions[tag_lower])

    # Step 3: top-level alias lookup
    if tag_lower in top_aliases:
        resolved = top_aliases[tag_lower]
        if resolved in entities:
            return ("alias_entity", resolved, entities[resolved])
        if resolved in actions:
            return ("alias_action", resolved, actions[resolved])

    # Step 4: per-entity alias scan
    for ent_key, ent_def in entities.items():
        for alias in (ent_def.get("aliases") or []):
            if tag_lower == alias.lower():
                return ("entity", ent_key, ent_def)
    for act_key, act_def in actions.items():
        for alias in (act_def.get("aliases") or []):
            if tag_lower == alias.lower():
                return ("action", act_key, act_def)

    # Step 5: difflib fuzzy match ≥0.72
    all_keys = list(entities.keys()) + list(actions.keys())
    all_aliases = list(top_aliases.keys())
    candidates = all_keys + all_aliases
    matches = difflib.get_close_matches(tag_lower, candidates, n=1, cutoff=0.72)
    if matches:
        best = matches[0]
        if best in entities:
            return ("fuzzy", best, entities[best])
        if best in actions:
            return ("fuzzy", best, actions[best])
        if best in top_aliases:
            resolved = top_aliases[best]
            if resolved in entities:
                return ("fuzzy", resolved, entities[resolved])

    # Step 6: unknown
    return ("unknown", tag_lower, {})


def build_outcomes(tags: list[str], refs: dict, namespace: str, actor: "str | None" = None) -> dict:
    """
    Resolve all tags and compose outcomes.

    Returns:
      {
        "entities": {key: def, ...},
        "actions":  {key: def, ...},
        "unknown":  [tag, ...],
        "outcomes": [{entity, action, file_to, skill, ...}, ...],
        "llm_calls": 0,
      }
    """
    resolved_entities = {}
    resolved_actions = {}
    resolved_routes = {}  # exact route matches — bypass entity/action/fuzzy chain
    unknown_tags = []

    resolved_areas = {}   # tag → folder path for member-area matches
    action_orig_tags = {}  # act_key → original tag text (for area lookup in action-only path)
    for tag in tags:
        # Exact route match wins over entity/action/fuzzy — prevents e.g. #tax26
        # from fuzzy-collapsing into the 'tax' action.
        if tag.lower() in refs.get("routes", {}):
            resolved_routes[tag.lower()] = resolve_route_keyword(tag.lower(), refs)
            continue
        kind, key, defn = resolve_tag(tag, refs)
        if kind in ("entity", "alias_entity", "fuzzy") and key not in resolved_actions:
            # check if it resolved to an entity
            if key in refs["entities"] or (kind == "fuzzy" and defn.get("type")):
                resolved_entities[key] = defn
            elif key in refs["actions"]:
                resolved_actions[key] = defn
                action_orig_tags[key] = tag.lower()
        elif kind in ("action", "alias_action"):
            resolved_actions[key] = defn
            action_orig_tags[key] = tag.lower()
        elif kind == "fuzzy":
            # fuzzy match — try to classify
            if key in refs["entities"]:
                resolved_entities[key] = defn
            elif key in refs["actions"]:
                resolved_actions[key] = defn
                action_orig_tags[key] = tag.lower()
        elif kind == "unknown":
            # Member-area lookup (step 3): only for tags unknown to entity/action/route
            # chain, so entity+action pairings (e.g. flo172+tax) are not disrupted.
            if actor:
                area_path = resolve_area_tag(tag, actor, refs)
                if area_path:
                    resolved_areas[tag.lower()] = area_path
                    continue
            unknown_tags.append(tag)

    # When entities are present, route-bypass tags that are also actions should
    # participate in entity+action pairing rather than generating standalone routes.
    if resolved_entities:
        for rt in list(resolved_routes.keys()):
            if rt in refs.get("actions", {}):
                resolved_actions[rt] = refs["actions"][rt]
                del resolved_routes[rt]

    # Re-resolve: for fuzzy that resolved to an entity key
    # Compose outcomes: each (entity, action) pair
    outcomes = []
    if resolved_entities and resolved_actions:
        for ent_key, ent_def in resolved_entities.items():
            for act_key, act_def in resolved_actions.items():
                outcome = {
                    "entity": ent_key,
                    "entity_def": ent_def,
                    "action": act_key,
                    "action_def": act_def,
                }
                # Resolve template strings
                file_to = (act_def.get("file_to") or "").replace(
                    "{entity.vault_path}", ent_def.get("vault_path", "")
                ).replace("{entity.ledger}", ent_def.get("ledger", "")).replace(
                    "{namespace}", namespace
                )
                outcome["file_to"] = file_to
                outcome["skill"] = act_def.get("skill", "")
                task_tpl = act_def.get("task_template")
                if task_tpl:
                    outcome["task"] = task_tpl.replace("{entity.label}", ent_def.get("label", ent_key))
                outcomes.append(outcome)
    elif resolved_entities and not resolved_actions:
        # Entity only — no action, just record entity resolve
        for ent_key, ent_def in resolved_entities.items():
            outcomes.append({"entity": ent_key, "entity_def": ent_def, "action": None})
    elif resolved_actions and not resolved_entities:
        # Action only (e.g. idea, followup don't need entity).
        # Resolution priority: global route > member-area > action template.
        for act_key, act_def in resolved_actions.items():
            route_path = resolve_route_keyword(act_key, refs)
            if route_path:
                file_to = route_path
            elif actor:
                # Member-area lookup: try original tag first (e.g. "finance" for tsu
                # → "Finance & Tax"), then the resolved action key as fallback.
                orig = action_orig_tags.get(act_key, act_key)
                area_path = resolve_area_tag(orig, actor, refs) or resolve_area_tag(act_key, actor, refs)
                file_to = area_path if area_path else ""
                if not file_to:
                    file_to = (act_def.get("file_to") or "")
                    file_to = re.sub(r"\{entity\.[a-zA-Z_]+\}/?", "", file_to)
                    file_to = file_to.replace("{namespace}", namespace)
                    file_to = re.sub(r"/{2,}", "/", file_to).lstrip("/")
            else:
                file_to = (act_def.get("file_to") or "")
                # Strip any {entity.*} placeholders — without a matched entity
                # they'd otherwise be created as literal directory names on
                # disk (e.g. `<vault>/{entity.vault_path}/tax/`). Collapse the
                # doubled slashes left behind.
                file_to = re.sub(r"\{entity\.[a-zA-Z_]+\}/?", "", file_to)
                file_to = file_to.replace("{namespace}", namespace)
                file_to = re.sub(r"/{2,}", "/", file_to).lstrip("/")
            outcomes.append({
                "entity": None,
                "action": act_key,
                "action_def": act_def,
                "file_to": file_to,
                "skill": act_def.get("skill", ""),
            })

    for route_tag, route_path in resolved_routes.items():
        outcomes.append({
            "entity": None,
            "action": None,
            "action_def": {},
            "file_to": route_path,
            "skill": None,
        })

    for area_tag, area_path in resolved_areas.items():
        outcomes.append({
            "entity": None,
            "action": None,
            "action_def": {},
            "file_to": area_path,
            "skill": None,
        })

    return {
        "entities": resolved_entities,
        "actions": resolved_actions,
        "unknown": unknown_tags,
        "outcomes": outcomes,
        "llm_calls": 0,
    }


def log_unknown_tags(tags: list[str], vault: Path) -> None:
    """Append unknown tags to .memory/unknown-tags.md."""
    if not tags:
        return
    unknown_file = vault / "99-System" / ".memory" / "unknown-tags.md"
    unknown_file.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(unknown_file, "a") as f:
        for tag in tags:
            f.write(f"- `{tag}` — first seen {now}\n")


def route(tags: list[str], namespace: str, vault: Optional[Path] = None,
          dry_run: bool = False, actor: "str | None" = None) -> dict:
    """
    Main entry point.
    Resolves tags, builds outcomes, logs unknowns.
    Returns outcome dict (llm_calls always 0).
    """
    if vault is None:
        vault = _vault_path()
    refs = load_references(vault)
    result = build_outcomes(tags, refs, namespace, actor=actor or namespace)

    if not dry_run:
        log_unknown_tags(result["unknown"], vault)

    return result


def resolve_area_tag(tag: str, actor: str, refs: dict) -> "str | None":
    """Return folder path relative to actor's vault, or None.

    1. Apply areas.shortcuts (e.g. 'fin' → 'finance').
    2. Look up canonical key in areas.members.<actor>.
    """
    areas = refs.get("areas") or {}
    shortcuts = areas.get("shortcuts") or {}
    members = areas.get("members") or {}
    canonical = shortcuts.get(tag.lower(), tag.lower())
    member_areas = members.get(actor.lower()) or {}
    return member_areas.get(canonical) or None


def preview_destination(tags: list, namespace: str) -> dict:
    """Sensor-safe preview of where a drop will land. No vault access, no logging.

    Combines `tags` and any hash_tags the caller has already merged. Used by the
    drop_file staging reply so the user sees the target folder before the
    executor moves the file. Mirrors the resolution executor.drop_file performs
    on the legacy path, plus an extra hint for entity-only matches that the
    executor currently dumps into 00-Inbox.

    Returns:
      {
        "dest_rel": str,             # e.g. "_shared/properties/flo172/expenses"
        "vault_owner": str,          # "shared" if action requests shared root, else namespace
        "routed": bool,              # False → 00-Inbox fallback
        "unknown": [tag, ...],       # tags that didn't resolve to anything
        "entity_label": str | None,  # human label of resolved entity, if any
        "needs_action": bool,        # entity matched but no action tag → 00-Inbox
      }
    """
    refs = load_references()
    res = build_outcomes(tags or [], refs, namespace, actor=namespace)
    outcomes = res.get("outcomes") or []
    unknown = res.get("unknown") or []
    entities = res.get("entities") or {}

    entity_label = None
    if entities:
        first_ent = next(iter(entities.values()))
        entity_label = first_ent.get("label")

    for o in outcomes:
        ft = (o.get("file_to") or "").strip().rstrip("/")
        if ft:
            act_def = o.get("action_def") or {}
            return {
                "dest_rel": ft,
                "vault_owner": "shared" if act_def.get("vault_root") == "shared" else namespace,
                "routed": True,
                "unknown": unknown,
                "entity_label": entity_label,
                "needs_action": False,
            }

    return {
        "dest_rel": "00-Inbox",
        "vault_owner": namespace,
        "routed": False,
        "unknown": unknown,
        "entity_label": entity_label,
        "needs_action": bool(entities),
    }


def known_areas_for(actor: str) -> set:
    """Return the set of valid area keywords for a member from references.yaml."""
    refs = load_references()
    areas = refs.get("areas") or {}
    members = areas.get("members") or {}
    shortcuts = areas.get("shortcuts") or {}
    member_areas = members.get(actor.lower()) or {}
    keys = set(member_areas.keys())
    # Include shortcut keys that resolve to a declared area for this member
    for short, canonical in shortcuts.items():
        if canonical in member_areas:
            keys.add(short)
    return keys


def resolve_route_keyword(keyword: str, refs: dict) -> "str | None":
    """Check if a keyword has a direct route in references.yaml.

    Routes support two forms:
      routes:
        tax: "finance/tax"           # string → path only
        ortho:                       # dict → path + optional owner
          path: health/ortho
          owner: ari

    Returns the path string or None.
    """
    routes = refs.get("routes") or {}
    route_def = routes.get(keyword.lower())
    if route_def is None:
        return None
    path_tpl = route_def if isinstance(route_def, str) else route_def.get("path", "")
    return path_tpl


def resolve_route_owner(keyword: str, refs: dict) -> "str | None":
    """Return the vault owner for a route, or None if not set.

    Only dict-form routes can have an owner:
      routes:
        ortho:
          path: health/ortho
          owner: ari     ← returned here
    """
    routes = refs.get("routes") or {}
    route_def = routes.get(keyword.lower())
    if not isinstance(route_def, dict):
        return None
    return route_def.get("owner") or None


def _find_references_file(vault_root: "Path | None" = None) -> "Path | None":
    """Return the references.yaml path (vault_root ignored, kept for compat)."""
    f = _references_file()
    return f if f.exists() else None


def resolve_topic_alias(topic: str, refs: dict) -> str:
    """Resolve a note topic through the aliases section of references.yaml.

    Returns the canonical topic name (lowercased), or the original if no alias found.
    Example: "jp" -> "2607-japan-china" if aliases: { jp: "2607-japan-china" }
    """
    topic_lower = topic.lower()
    aliases = refs.get("aliases") or {}
    return aliases.get(topic_lower, topic_lower)


def resolve_topic_route(topic: str, refs: dict) -> "str | None":
    """Check if a topic (after alias resolution) has a route in references.yaml.

    Returns the route path (e.g. "health/ortho") or None if unrouted.
    Routed topics sync to {route}/_context.md in the vault.
    """
    return resolve_route_keyword(topic, refs)


def load_sensor_references() -> dict:
    """Load references.yaml — same file on both sensor (VPS) and executor (Mac).

    Now that references.yaml lives in $AAKA_CONFIG_DIR/config/, both sides read
    the same path. No sync step needed.
    """
    return load_references()


def tag_for_gmail_label(label_name: str) -> "dict | None":
    """Return route info for a tag that has gmail_label matching label_name.

    Scans routes in references.yaml for entries with ``gmail_label: <label_name>``.
    Returns {tag_name, path, owner} or None.

    Configure in references.yaml:
        routes:
          payslips:
            path: finance/payslips
            owner: alex
            gmail_label: "HR/Payslips"   # attachments auto-filed to this route
    """
    refs = load_references()
    routes = refs.get("routes") or {}
    for tag_name, route in routes.items():
        if isinstance(route, dict) and route.get("gmail_label") == label_name:
            return {
                "tag_name": tag_name,
                "path": route.get("path", ""),
                "owner": route.get("owner", ""),
            }
    return None


def write_tag_to_references(vault_root: "Path | None" = None,
                             alias: "str | None" = None,
                             alias_target: "str | None" = None,
                             route_key: "str | None" = None,
                             route_path: "str | None" = None,
                             route_owner: "str | None" = None,
                             description_key: "str | None" = None,
                             description_value: "str | None" = None) -> bool:
    """Write an alias or route entry to $AAKA_CONFIG_DIR/config/references.yaml.

    vault_root is ignored (kept for backwards compatibility).

    If route_owner is provided, the route is stored as a dict:
      routes:
        ortho:
          path: health/ortho
          owner: ari

    Otherwise stored as a plain string: ortho: health/ortho

    Returns True if the file was modified.
    """
    ref_file = _references_file()
    ref_file.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    if ref_file.exists():
        with open(ref_file) as f:
            data = yaml.safe_load(f) or {}

    modified = False
    if alias and alias_target:
        aliases = data.setdefault("aliases", {})
        if aliases.get(alias.lower()) != alias_target.lower():
            aliases[alias.lower()] = alias_target.lower()
            modified = True
    if route_key and route_path:
        routes = data.setdefault("routes", {})
        key = route_key.lower()
        new_val = {"path": route_path, "owner": route_owner} if route_owner else route_path
        if routes.get(key) != new_val:
            routes[key] = new_val
            modified = True
    if description_key and description_value is not None:
        descs = data.setdefault("descriptions", {})
        if descs.get(description_key.lower()) != description_value:
            descs[description_key.lower()] = description_value
            modified = True

    if modified:
        with open(ref_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return modified


def auto_learn_route(tag: str, area: str, vault_root: "Path | None" = None) -> bool:
    """Auto-register a #tag → area route in references.yaml.

    When a user drops f ari health #ortho, 'ortho' gets auto-added as:
      routes:
        ortho: health/ortho

    vault_root is ignored — writes to $AAKA_CONFIG_DIR/config/references.yaml.
    Returns True if a new route was added, False if already exists.
    """
    ref_file = _references_file()
    ref_file.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    if ref_file.exists():
        with open(ref_file) as f:
            data = yaml.safe_load(f) or {}

    routes = data.setdefault("routes", {})
    tag_lower = tag.lower()
    if tag_lower in routes:
        return False  # already known

    routes[tag_lower] = f"{area}/{tag_lower}"
    with open(ref_file, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return True


def retire_tag(tag: str) -> dict:
    """Remove alias and route entries for a tag from references.yaml.

    Frees the tag name for reuse (e.g. 'jp' can be reassigned to a new trip).
    The vault folder and _context.md are NOT touched — they remain as archive.

    Returns:
      {
        "found": bool,
        "removed_alias": str | None,   # the alias target that was removed
        "removed_route": str | None,   # the route path that was removed
        "removed_owner": str | None,   # the owner that was removed
      }
    """
    ref_file = _references_file()
    if not ref_file.exists():
        return {"found": False, "removed_alias": None, "removed_route": None, "removed_owner": None}

    with open(ref_file) as f:
        data = yaml.safe_load(f) or {}

    tag_lower = tag.lower()
    modified = False
    removed_alias = None
    removed_route = None
    removed_owner = None

    aliases = data.get("aliases") or {}
    routes = data.get("routes") or {}

    # Determine canonical name: if tag is an alias, the canonical is its target
    canonical = aliases.get(tag_lower, tag_lower)

    # Remove alias entry (tag → canonical)
    if tag_lower in aliases:
        removed_alias = aliases.pop(tag_lower)
        data["aliases"] = aliases
        modified = True

    # Also remove reverse aliases (canonical → tag, rare but possible)
    for k, v in list(aliases.items()):
        if v == tag_lower or v == canonical:
            aliases.pop(k)
            modified = True

    # Remove route stored under canonical name (or tag_lower if no alias)
    for route_key in {tag_lower, canonical}:
        if route_key in routes:
            route_def = routes.pop(route_key)
            if isinstance(route_def, dict):
                removed_route = route_def.get("path")
                removed_owner = route_def.get("owner")
            else:
                removed_route = route_def
            data["routes"] = routes
            modified = True
            break

    found = modified
    if modified:
        with open(ref_file, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return {
        "found": found,
        "removed_alias": removed_alias,
        "removed_route": removed_route,
        "removed_owner": removed_owner,
    }


def resolve_smart_drop(actor: str, area: "str | None", hash_tags: list,
                       custom_name: "str | None", file_count: int,
                       tags: "list | None" = None) -> dict:
    """Zero-LLM folder resolution for Smart Drop.

    vault_path_for(actor) already returns the member's own vault root
    (e.g. ~/aaka-vault/<member_id>), so paths are relative to that —
    no {actor} subdirectory needed.

    Resolution chain:
      1. actor's vault_root via aaka_config.vault_path_for(actor)
      2. Check tags against global routes (tax25, den40, …)
      3. Check hash_tags against global routes
      4. Member-area lookup via tags (health → "14 Health" for alex)
      5. Member-area lookup via hash_tags
      6. Explicit area= param via member-area lookup
      7. Fallback: 00-Inbox/

    Returns:
      {
        "vault_root": Path,
        "dest_rel": str,           # e.g. "health/ortho"
        "subfolder": str | None,
        "create_dirs": [str],
        "learned": [str],
        "error": str | None,
      }
    """
    import aaka_config as _cfg

    vault_root = _cfg.vault_path_for(actor)
    if vault_root is None:
        return {
            "vault_root": None,
            "dest_rel": "00-Inbox",
            "subfolder": None,
            "create_dirs": [],
            "learned": [],
            "error": f"No vault_path configured for member '{actor}' — file will go to sender's 00-Inbox",
        }

    learned = []
    refs = load_references(vault_root)

    def _append_multi(dest_rel):
        """Append hash_tags and optional custom_name subfolder."""
        nonlocal hash_tags, custom_name, file_count
        for ht in hash_tags:
            dest_rel += f"/{ht}"
        subfolder = None
        if custom_name and file_count > 1:
            from tools.file_utils import sanitize_filename
            safe = sanitize_filename(custom_name + ".tmp").replace(".tmp", "")
            if safe:
                subfolder = safe
                dest_rel += f"/{safe}"
        return dest_rel, subfolder

    # Try route keywords from tags (e.g. "tax25" → "11 Finance/...")
    for t in list(tags or []):
        routed_path = resolve_route_keyword(t, refs)
        if routed_path:
            dest_rel, subfolder = _append_multi(routed_path)
            return {
                "vault_root": vault_root, "dest_rel": dest_rel,
                "subfolder": subfolder, "create_dirs": [],
                "learned": [], "error": None,
            }

    # Check hash_tags against learned routes (e.g. #ortho → "health/ortho")
    for ht in list(hash_tags):
        routed_path = resolve_route_keyword(ht, refs)
        if routed_path:
            remaining_hts = [h for h in hash_tags if h != ht]
            dest_rel = routed_path
            for rh in remaining_hts:
                dest_rel += f"/{rh}"
            subfolder = None
            if custom_name and file_count > 1:
                from tools.file_utils import sanitize_filename
                safe = sanitize_filename(custom_name + ".tmp").replace(".tmp", "")
                if safe:
                    subfolder = safe
                    dest_rel += f"/{safe}"
            return {
                "vault_root": vault_root, "dest_rel": dest_rel,
                "subfolder": subfolder, "create_dirs": [],
                "learned": [], "error": None,
            }

    # Member-area lookup via tags (e.g. "health" for alex → "14 Health")
    for t in list(tags or []):
        area_path = resolve_area_tag(t, actor, refs)
        if area_path:
            dest_rel, subfolder = _append_multi(area_path)
            return {
                "vault_root": vault_root, "dest_rel": dest_rel,
                "subfolder": subfolder, "create_dirs": [],
                "learned": [], "error": None,
            }

    # Member-area lookup via hash_tags
    for ht in list(hash_tags):
        area_path = resolve_area_tag(ht, actor, refs)
        if area_path:
            remaining_hts = [h for h in hash_tags if h != ht]
            dest_rel = area_path
            for rh in remaining_hts:
                dest_rel += f"/{rh}"
            subfolder = None
            if custom_name and file_count > 1:
                from tools.file_utils import sanitize_filename
                safe = sanitize_filename(custom_name + ".tmp").replace(".tmp", "")
                if safe:
                    subfolder = safe
                    dest_rel += f"/{safe}"
            return {
                "vault_root": vault_root, "dest_rel": dest_rel,
                "subfolder": subfolder, "create_dirs": [],
                "learned": [], "error": None,
            }

    # Explicit area= param via member-area lookup (replaces dead STANDARD_AREAS check)
    if area:
        area_path = resolve_area_tag(area, actor, refs)
        if area_path:
            parts = [area_path]
            new_dirs = []
            for ht in hash_tags:
                parts.append(ht)
                new_dirs.append("/".join(parts))
                try:
                    if auto_learn_route(ht, area_path, vault_root):
                        learned.append(ht)
                except Exception:
                    pass
        else:
            parts = ["00-Inbox"]
            new_dirs = []
    else:
        # No area, no route match → inbox
        parts = ["00-Inbox"]
        new_dirs = []

    dest_rel = "/".join(parts)
    subfolder = None
    if custom_name and file_count > 1:
        from tools.file_utils import sanitize_filename
        safe = sanitize_filename(custom_name + ".tmp").replace(".tmp", "")
        if safe:
            subfolder = safe
            dest_rel = dest_rel + "/" + safe
            new_dirs.append(dest_rel)

    return {
        "vault_root": vault_root,
        "dest_rel": dest_rel,
        "subfolder": subfolder,
        "create_dirs": new_dirs,
        "learned": learned,
        "error": None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aaka inbox router — zero-LLM tag resolution")
    parser.add_argument("--tags", required=True, help="Space-separated tags, e.g. 'flo172 tax expense'")
    parser.add_argument("--namespace", default="alice", help="User namespace (member ID from aaka.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write unknown-tags.md")
    args = parser.parse_args()

    tag_list = args.tags.lower().split()
    result = route(tag_list, namespace=args.namespace, dry_run=args.dry_run)

    print(f"Entities resolved : {list(result['entities'].keys())}")
    print(f"Actions resolved  : {list(result['actions'].keys())}")
    print(f"Unknown tags      : {result['unknown']}")
    print(f"LLM calls         : {result['llm_calls']}")
    print(f"Outcomes ({len(result['outcomes'])}):")
    for o in result["outcomes"]:
        print(f"  entity={o.get('entity')}  action={o.get('action')}  "
              f"file_to={o.get('file_to','')}  skill={o.get('skill','')}")
