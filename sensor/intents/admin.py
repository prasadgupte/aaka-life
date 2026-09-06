"""sensor/intents/admin.py — Admin and status handlers (health, menu, members, llm, senders)."""
import re
import os
from pathlib import Path

import aaka_config

HANDLES = frozenset({
    "health_check", "menu", "members_list", "llm_call",
    "approve_sender", "deny_sender", "errors_report",
})


def handle(intent: str, message: str, sender: str, channel_id: str, source: str) -> str:
    if intent == "health_check":
        if re.search(r'\btrace\b', message.lower()):
            m_hash = re.search(r'#([0-9a-f]{8,})', message.lower())
            if not m_hash:
                from aaka_config import reply_prefix
                return f"{reply_prefix()}Usage: /status trace #<hash>"
            from gateway.ingress import trace as _ingress_trace
            entries = _ingress_trace(m_hash.group(1))
            if not entries:
                return f"No trace found for #{m_hash.group(1)}"
            lines = [f"Trace #{m_hash.group(1)}:"]
            for e in entries:
                label = "→" if e["_log"] == "egress.jsonl" else "←"
                lines.append(f"  {label} {e.get('ts','')} [{e.get('source','?')}] "
                              f"{e.get('status','?')} {e.get('intent','')}")
            return "\n".join(lines)
        if re.search(r'\begress\b', message.lower()):
            from gateway.egress import egress_status
            s = egress_status(hours=1)
            lines = [
                f"{'🟢' if s['enabled'] else '🔴'} Egress {'enabled' if s['enabled'] else 'KILLED'}",
                f"Last 1h: {s['total']} sends  {s['by_status']}",
            ]
            if s["by_recipient"]:
                top = sorted(s["by_recipient"].items(), key=lambda x: -x[1])[:5]
                lines.append("Recipients: " + ", ".join(f"{r}×{n}" for r, n in top))
            if s["recent_errors"]:
                lines.append("Recent errors:")
                for e in s["recent_errors"]:
                    lines.append(f"  {e.get('ts','')} [{e.get('source','')}] {e.get('error','')[:80]}")
            return "\n".join(lines)
        if re.search(r'\bcode\b', message.lower()):
            from skills.status.status_core import status_code
            return status_code()
        if re.search(r'\bqueue\b', message.lower()):
            from skills.status.status_core import status_queue
            return status_queue()
        if re.search(r'\bllm\b', message.lower()):
            from skills.status.llm import status_llm
            return status_llm(ping=True)
        if re.search(r'\bcron\b', message.lower()):
            from sensor.router_sensor import _handle_status_cron
            return _handle_status_cron()
        audit = aaka_config.LOGS_DIR / "audit.log"
        lines = audit.read_text().strip().split("\n")[-5:] if audit.exists() else ["(no log)"]
        status = f"🟢 {aaka_config.bot_emoji()} {aaka_config.bot_name()} healthy.\n\nRecent log:\n" + "\n".join(lines)
        try:
            import json as _json, time as _time
            _li_tokens = aaka_config.secrets_root() / "linkedin-tool" / "tokens.json"
            if _li_tokens.exists():
                _t = _json.loads(_li_tokens.read_text())
                _expiry = _t.get("saved_at", 0) + _t.get("expires_in", 0)
                _days_left = int((_expiry - _time.time()) / 86400)
                if _days_left <= 14:
                    status += f"\n\n⚠️ LinkedIn token expires in {_days_left} day(s) — run /li-reauth"
        except Exception:
            pass
        return status

    if intent == "menu":
        return (
            "*Commands*\n"
            "\n"
            "📅  d · /today · /week · /day Mon    +#fix · issues\n"
            "     /cal · add event    c fix · all issues\n"
            "     c fix N name · assign    c fix N #work · busy\n"
            "     /plan · find slots    /block · block time\n"
            "\n"
            "📋  t · /tasks · list    t text · add task\n"
            "     /done N · complete    /done 1 3 · bulk\n"
            "     /snooze N 3d · snooze    /snooze all 1d\n"
            "     /edit N · update    /del N · remove\n"
            "     /tasks #tag · filter    /done today · history\n"
            "\n"
            "📝  n tag · read    n tag text · write\n"
            "     n member tag text · write to member (admin only)\n"
            "     n tag !name text · set canonical filename alias\n"
            "     f [member] [area] [#tag] [\"name\"]. desc · smart drop\n"
            "     e.g. f ari health #ortho \"doc\". checkup\n"
            "     /tags · all tags    /tag · manage aliases+routes    f help · drop options\n"
            "\n"
            "📄  p compress · shrink PDF    p extract · text + meta    p ocr · OCR scanned PDF\n"
            "     p split 2s · split    p merge · merge staged\n"
            "     p delete 3-5 · remove pages    p delete blank-pages · auto-remove blanks\n"
            "     append `ocr` to any command to OCR its output last    p help · all PDF ops\n"
            "\n"
            "🛒  b list · show    b list item · add    b list ? · ideas (add N N)\n"
            "💸  pay NAME IBAN AMOUNT REF · SEPA QR\n"
            "💰  x · month    x 45 groceries lidl · log\n"
            "🎂  bday · upcoming    bday N · wish link\n"
            "↩️  undo #hash · cancel/undo queued action\n"
            "     keep #hash · restore uncompressed original of a /drop\n"
            "👤  /approve <id> · allow sender    /deny <id> · block\n"
            "⚙️  /status · health    q · queue    /llm · ask AI\n"
            "     /errors · unacked errors    /errors flush · clear\n"
            "🛠  /tools · list/run tools    /tools/<tool> help\n"
            "     /members · roster    /invite <name> · onboard (admin)\n"
            "     /mcp · introspection — members/tools/bot/setup (admin)\n"
            "     /security · hardening self-audit    /security surface · exposure map (admin)\n"
            "     /ask <agent> <request> · page a local agent, get a reply + files (admin)\n"
            "📊  /engage · your level & what to try next\n"
            "\n"
            "📬  m · /mail · accounts    m name · messages\n"
            "     m read name N · read    /mail fetch · trigger fetch\n"
            "🔑  /pw list [group] · entries    /pw search X · find"
        )

    if intent == "members_list":
        sender_member = aaka_config.member_by_sender(sender)
        sender_id = sender_member["id"] if sender_member else None
        if not aaka_config.member_is_admin(sender_id):
            return "🔒 Admin only."
        from sensor.router_sensor import _format_members
        return _format_members()

    if intent == "llm_call":
        from llm import call_llm
        prompt = re.sub(r'^/llm\s*', '', message, flags=re.I).strip()
        if not prompt:
            return "Usage: /llm <your prompt>"
        try:
            reply = call_llm(prompt)
            return f"🤖 {reply.strip()}"
        except Exception as exc:
            return f"⚠️ LLM error: {exc}"

    if intent == "approve_sender":
        return _handle_approve_sender(message, requester=sender)

    if intent == "deny_sender":
        return _handle_deny_sender(message, requester=sender)

    if intent == "errors_report":
        member = aaka_config.member_by_sender(sender)
        if not aaka_config.member_is_admin(member.get("id") if member else None):
            return "🔒 Admin only."
        from sensor.error_digest import get_unacked_summary, ack_all, scan_and_store
        if re.search(r'\bflush\b', message, re.I):
            n = ack_all()
            return f"✅ Flushed {n} error event(s). Next digest starts clean."
        if re.search(r'\bscan\b', message, re.I):
            counts = scan_and_store(hours=24)
            total = sum(counts.values())
            return f"🔍 Scanned logs: {total} error(s) stored."
        summary = get_unacked_summary()
        if not summary:
            return "✅ No unacknowledged errors."
        _LABELS = {
            "tg_delivery": "TG delivery failures",
            "tg_other": "TG other errors",
            "dispatch_error": "Dispatch errors/timeouts",
            "poller_restart": "Poller restarts",
            "cal_sync": "Calendar sync 403/404",
            "cal_other": "Calendar sync errors",
            "outbox_error": "Outbox failures",
            "other": "Other errors",
        }
        total = sum(e["count"] for e in summary)
        lines = [f"*Unacknowledged errors* ({total} total):"]
        for e in summary:
            label = _LABELS.get(e["category"], e["category"])
            since = e["first_seen"][:10] if e["first_seen"] else "?"
            lines.append(f"• {label}: {e['count']} (since {since})")
        lines.append("\nReply `/errors flush` to clear.")
        return "\n".join(lines)

    return "❓ Unknown admin intent."


# ── Sender approval / denial helpers ─────────────────────────────────────────

def _cancel_sender_approvals(sender_id: str) -> int:
    from aaka_queue.queue import _connect
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _connect()
    r = conn.execute(
        "UPDATE queue_items SET status='cancelled', updated_at=? "
        "WHERE status='awaiting_confirm' AND sender=?",
        (now, sender_id),
    )
    conn.commit()
    return r.rowcount


def _write_sender_list(filename: str, sender_id: str) -> None:
    import json as _json
    data_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config")) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / filename
    data: dict = {}
    if path.exists():
        try:
            data = _json.loads(path.read_text())
        except Exception:
            pass
    data[sender_id] = True
    path.write_text(_json.dumps(data, indent=2))


def _handle_approve_sender(message: str, requester: str) -> str:
    member = aaka_config.member_by_sender(requester)
    if not aaka_config.member_is_admin(member.get("id") if member else None):
        return "🔒 Admin only."
    parts = message.strip().split(None, 1)
    if len(parts) < 2:
        return "Usage: /approve <sender_id>"
    sender_id = parts[1].strip()
    _write_sender_list("approved_senders.json", sender_id)
    count = _cancel_sender_approvals(sender_id)
    return (
        f"✅ {sender_id} approved.\n"
        f"Cleared {count} pending request(s).\n"
        "To grant full access, add them to aaka.yaml."
    )


def _handle_deny_sender(message: str, requester: str) -> str:
    member = aaka_config.member_by_sender(requester)
    if not aaka_config.member_is_admin(member.get("id") if member else None):
        return "🔒 Admin only."
    parts = message.strip().split(None, 1)
    if len(parts) < 2:
        return "Usage: /deny <sender_id>"
    sender_id = parts[1].strip()
    _write_sender_list("blocked_senders.json", sender_id)
    count = _cancel_sender_approvals(sender_id)
    return (
        f"🚫 {sender_id} blocked.\n"
        f"Cleared {count} pending request(s).\n"
        "They will be silently ignored from now on."
    )
