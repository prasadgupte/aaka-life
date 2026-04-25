"""sensor/intents/notes.py — Local note read handler."""
import re

import aaka_config

HANDLES = frozenset({"read_note"})


def handle(intent: str, message: str, sender: str, channel_id: str, source: str) -> str:
    if intent == "read_note":
        from sensor.router_sensor import _update_sync_marker
        notes_dir = aaka_config.DATA_DIR / "notes"
        arg = re.sub(r'^/notes\s*|^notes\s*', '', message, flags=re.I).strip()
        words = arg.split() if arg else []
        topic = words[0].lower() if words else ""
        try:
            limit = int(words[1]) if len(words) >= 2 else 10
        except ValueError:
            limit = 10
        limit = min(limit, 50)

        if not topic:
            if not notes_dir.exists():
                return "📝 No notes yet. Write one with: n <topic> <text>"
            topics = sorted(p.stem for p in notes_dir.glob("*.md"))
            if not topics:
                return "📝 No notes yet. Write one with: n <topic> <text>"
            return "📝 *Note topics:*\n" + "\n".join(f"  #{t}" for t in topics) + "\n\n↪ /notes <topic> to read"

        try:
            from skills.notes.note_writer import resolve_alias as _resolve_note_alias
            topic = _resolve_note_alias(topic)
        except Exception:
            pass

        note_path = notes_dir / f"{topic}.md"
        if not note_path.exists():
            available = sorted(p.stem for p in notes_dir.glob("*.md")) if notes_dir.exists() else []
            hint = "  Topics: " + ", ".join(f"#{t}" for t in available) if available else ""
            return f"📝 No notes for #{topic}.{chr(10) + hint if hint else ''}"

        _update_sync_marker(topic, note_path)

        lines = note_path.read_text().splitlines()
        MARKER = "--- synced ---"
        all_entries = [l for l in lines if l.startswith("- ")]
        if not all_entries:
            return f"📝 #{topic} — (empty)"

        marker_idx = next((i for i, l in enumerate(lines) if l.strip() == MARKER), None)
        if marker_idx is not None:
            pending = [l for l in lines[marker_idx + 1:] if l.startswith("- ")]
            if not pending:
                return (f"📝 *#{topic}* — ✅ all {len(all_entries)} synced to executor"
                        f"\n\n↪ n {topic} <text> to add")
            synced_count = len(all_entries) - len(pending)
            recent = pending[-limit:]
            header = f"📝 *#{topic}* — {len(pending)} pending · {synced_count} synced"
        else:
            recent = all_entries[-limit:]
            header = f"📝 *#{topic}* — last {len(recent)} of {len(all_entries)}"

        return header + "\n\n" + "\n".join(recent) + f"\n\n↪ n {topic} <text> to add · n {topic} {min(len(all_entries), 20)} to read more"

    return "❓ Unknown notes intent."
