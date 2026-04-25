"""sensor/intents/tags.py — Tag list and tag management handlers."""
import re

import aaka_config
from aaka_queue.queue import write_item, update_status

HANDLES = frozenset({"list_tags", "tag_manage"})


def handle(intent: str, message: str, sender: str, channel_id: str, source: str) -> str:
    if intent == "list_tags":
        import yaml as _yaml
        tags_file = aaka_config.CONFIG_DIR / "config" / "tags.yaml"
        if not tags_file.exists():
            return "🏷️ No tags configured."
        cfg = _yaml.safe_load(tags_file.read_text())
        tag_map = cfg.get("tag_map", {})
        if not tag_map:
            return "🏷️ No tags configured."
        _CATEGORY_LABELS = {
            "people": "👤 People",
            "properties": "🏠 Properties",
            "jurisdictions": "🌍 Jurisdictions",
            "document_types": "📄 Documents",
        }
        lines = ["🏷️ *Tags*", ""]
        for cat, entries in tag_map.items():
            if cat == "verb_triggers":
                pairs = " · ".join(f"{v}→{','.join(t)}" for v, t in entries.items())
                lines.append(f"⚡ Auto-tag: {pairs}")
            else:
                label = _CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
                if isinstance(entries, dict):
                    tags_str = " ".join(f"#{k}" for k in entries)
                else:
                    tags_str = " ".join(str(e) for e in entries) if entries else "(empty)"
                lines.append(f"{label}: {tags_str}")
        return "\n".join(lines)

    if intent == "tag_manage":
        import yaml as _yaml
        arg = re.sub(r'^/tag\s*', '', message, flags=re.I).strip()
        parts = arg.split(None, 2)

        try:
            from tools.inbox_router import load_sensor_references, resolve_topic_alias, resolve_topic_route
            _refs = load_sensor_references()
        except Exception:
            _refs = {"entities": {}, "actions": {}, "aliases": {}, "routes": {}}

        def _enqueue_tag_write(payload: dict) -> str:
            wi = write_item(
                intent="write_tag", raw_message=message,
                sender=sender, channel_id=channel_id, source=source,
                payload=payload,
            )
            update_status(wi, "confirmed")
            return wi

        if not arg or arg.lower() == "help":
            return (
                "🏷️ *Tag management*\n\n"
                "  /tag list                          — list all aliases + routes\n"
                "  /tag <name>                        — details for a tag\n"
                "  /tag <name> alias <target>         — create alias\n"
                "  /tag <name> route <path>           — set vault route (your vault)\n"
                "  /tag <name> route <path> owner <member>  — route to a member's vault\n"
                "  /tag <name> desc <text>            — set description\n"
                "  /tag <name> retire                      — free alias for reuse (keeps vault files)\n\n"
                "Once registered, just use the tag name — no need to specify the member:\n\n"
                "  /tag ortho route health/ortho owner ari\n"
                "    → n ortho first visit       ✅ Ari/health/ortho/_context.md\n"
                "    → f health #ortho \"doc\". checkup  ✅ same folder\n\n"
                "  /tag jp alias 2607-japan-china route travel/japan26\n"
                "    → n jp bought rail pass     ✅ your vault/travel/japan26/_context.md\n\n"
                "  /tag tax26 route finance/tax/2026\n"
                "    → n tax26 paid advance      ✅ your vault\n"
                "    → f tsu finance #tax26 \"form\". IRS  ✅ Tsu's vault\n\n"
                "  /tag list"
            )

        if parts[0].lower() == "list":
            aliases = _refs.get("aliases") or {}
            routes = _refs.get("routes") or {}
            descs = _refs.get("descriptions") or {}
            if not aliases and not routes:
                return "🏷️ No aliases or routes registered yet.\n\nUse /tag <name> alias <target> or /tag <name> route <path>"
            out = ["🏷️ *Tags & Routes*", ""]
            if aliases:
                out.append("*Aliases:*")
                for k, v in sorted(aliases.items()):
                    out.append(f"  {k} → {v}")
            if routes:
                if aliases:
                    out.append("")
                out.append("*Routes (vault folders):*")
                for k, v in sorted(routes.items()):
                    _desc = descs.get(k)
                    _desc_str = f"  — {_desc}" if _desc else ""
                    if isinstance(v, dict):
                        _path = v.get("path", "")
                        _owner = v.get("owner", "")
                        _owner_str = f" ({_owner})" if _owner else ""
                        out.append(f"  #{k} → {_owner.title() + '/' if _owner else ''}{_path}/_context.md{_owner_str}{_desc_str}")
                    else:
                        out.append(f"  #{k} → {v}/_context.md{_desc_str}")
            return "\n".join(out)

        tag_name = parts[0].lower()
        if len(parts) == 1:
            aliases = _refs.get("aliases") or {}
            routes = _refs.get("routes") or {}
            descs = _refs.get("descriptions") or {}
            canonical = resolve_topic_alias(tag_name, _refs) if aliases else tag_name
            route = resolve_topic_route(canonical, _refs) if routes else None
            _raw_route = (routes or {}).get(canonical)
            _owner = _raw_route.get("owner") if isinstance(_raw_route, dict) else None
            _desc = descs.get(canonical) or descs.get(tag_name)
            lines = [f"🏷️ *#{tag_name}*", ""]
            if _desc:
                lines.append(f"  {_desc}")
                lines.append("")
            if canonical != tag_name:
                lines.append(f"  Alias → #{canonical}")
            if route:
                _vault_prefix = f"{_owner.title()}/" if _owner else ""
                lines.append(f"  Route → {_vault_prefix}{route}/_context.md")
            if _owner:
                lines.append(f"  Owner → {_owner}")
            _sensor_note = aaka_config.DATA_DIR / "notes" / f"{canonical}.md"
            if _sensor_note.exists():
                _entry_count = sum(1 for l in _sensor_note.read_text().splitlines() if l.startswith("- "))
                lines.append(f"  Note entries: {_entry_count}")
            else:
                lines.append(f"  No note entries yet")
            if not route and canonical == tag_name:
                lines.append(f"\n  Tip: /tag {tag_name} route <area/folder> to assign a vault folder")
            return "\n".join(lines)

        if len(parts) >= 3:
            action = parts[1].lower()
            value = parts[2].strip()

            if action in ("alias", "a"):
                _enqueue_tag_write({"action": "alias", "tag_name": tag_name, "alias_target": value})
                return f"🏷️ Alias queued: #{tag_name} → #{value}\n\n↪ n {tag_name} <text> will use #{value} as the note file"
            elif action in ("route", "r", "path"):
                _route_owner = None
                _route_path = value
                _owner_match = re.search(r'\s+owner\s+(\S+)\s*$', value, re.I)
                if _owner_match:
                    _route_owner = _owner_match.group(1).lower()
                    _route_path = value[:_owner_match.start()].strip()
                    if not aaka_config.member_by_name(_route_owner):
                        return f"🏷️ Unknown member '{_route_owner}'. Check /members for valid names."
                _enqueue_tag_write({"action": "route", "tag_name": tag_name,
                                    "route_path": _route_path, "route_owner": _route_owner})
                _owner_note = f" (owner: {_route_owner})" if _route_owner else ""
                _vault_hint = f"{_route_owner.title() if _route_owner else 'your vault'}/{_route_path}/_context.md"
                return (f"🏷️ Route queued: #{tag_name} → {_vault_hint}{_owner_note}\n\n"
                        f"↪ `n {tag_name} <text>` writes there automatically (active in ~15s)")
            elif action in ("desc", "description", "d"):
                _enqueue_tag_write({"action": "desc", "tag_name": tag_name, "description": value})
                return f"🏷️ Description queued for #{tag_name}: {value}"
            elif action in ("retire", "remove", "delete", "rm"):
                _enqueue_tag_write({"action": "retire", "tag_name": tag_name})
                return (f"🏷️ #{tag_name} retirement queued.\n\n"
                        f"Alias + route will be freed in ~15s. Vault files kept as archive.")
            else:
                return f"🏷️ Unknown action '{action}'. Use: alias, route, desc, or retire\n\nExample: /tag {tag_name} desc <text>"

        return "🏷️ Usage: /tag <name> alias <target>  or  /tag <name> route <path>"

    return "❓ Unknown tags intent."
