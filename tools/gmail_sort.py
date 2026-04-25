#!/usr/bin/env python3
"""
tools/gmail_sort.py — bulk-sort Gmail using Haiku + per-sender memory.

Flow per message:
  1. Sender already decided        → apply cached label, archive
  2. Keyword/domain rule matches   → cache sender→label, apply, archive
  3. Haiku classifies confidently  → cache sender→label, apply, archive
  4. Uncertain                     → ask user via Telegram (non-blocking).
                                     Other messages from this sender are deferred
                                     and replayed once the answer arrives.

Promo / Newsletter senders also get their List-Unsubscribe URL written to
$AAKA_CONFIG_DIR/data/gmail_sort/unsub_<member>.md for batch unsubscribing.

Usage:
    python3 tools/gmail_sort.py                       # all members, all INBOX
    python3 tools/gmail_sort.py --member kiran        # single member
    python3 tools/gmail_sort.py --survey --max 1000   # header-only triage report
    python3 tools/gmail_sort.py --max 100             # cap per member
    python3 tools/gmail_sort.py --dry-run             # show planned actions
    python3 tools/gmail_sort.py --no-ask              # skip uncertain (no Telegram)
    python3 tools/gmail_sort.py --rebind-from-rules   # re-evaluate rules against
                                                       cached sender decisions

Rules file (PG-edited, gitignored):
    $AAKA_CONFIG_DIR/config/gmail_sort_rules.yaml
      rules:           keyword/domain → label mappings
      watch_keywords:  terms that pin the message to "Watch" (kept in INBOX)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import aaka_config
if "QUEUE_DB" not in os.environ:
    os.environ["QUEUE_DB"] = str(aaka_config.QUEUE_DIR / "butler.db")

from aaka_queue.queue import _connect
from gateway.agent_client import AakaClient, AakaClientError
from skills.mail.gmail import (
    get_gmail_service,
    list_labels,
    apply_labels,
    fetch_headers_full,
    fetch_full,
    create_label,
    parse_list_unsubscribe,
)


# ── Config / paths ─────────────────────────────────────────────────────────────

RULES_FILE = aaka_config.CONFIG_DIR / "config" / "gmail_sort_rules.yaml"
UNSUB_DIR = aaka_config.DATA_DIR / "gmail_sort"

DEFAULT_TAXONOMY = [
    "Newsletter", "Receipt", "Travel", "Work", "Personal",
    "Promo", "Notify", "Action", "Reference", "Watch",
]
PROMO_LIKE = {"Newsletter", "Promo", "Notify"}
WATCH_LABEL = "Watch"  # never archived — left in INBOX for review
CONFIDENCE_THRESHOLD = 0.75
BATCH_SIZE = 100
LLM_BATCH = 20
RATE_SLEEP = 0.05  # 50ms between Gmail writes

# HARD SAFETY RULE: this script must never move messages to TRASH.
# Any rule or decision with action="trash" is downgraded to applying the
# _TrashCandidate label + archiving — the user reviews and trashes manually.
# Do not parameterize this; it must not be toggleable.
NEVER_TRASH = True
TRASH_CANDIDATE_LABEL = "_TrashCandidate"


# ── ANSI ───────────────────────────────────────────────────────────────────────
_G = "\033[32m"; _Y = "\033[33m"; _C = "\033[36m"; _R = "\033[31m"
_D = "\033[2m"; _B = "\033[1m"; _N = "\033[0m"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_api_key() -> str:
    """Get AAKA_AGENT_KEY from env or the gmail-sorter secret file."""
    key = os.environ.get("AAKA_AGENT_KEY", "")
    if key:
        return key
    env_path = Path("/Users/Shared/secrets/aaka-repo/gmail-sorter.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("AAKA_AGENT_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _admin_telegram() -> str | None:
    """Telegram chat id of the first member with admin: true (PG)."""
    for m in aaka_config.members():
        if m.get("admin") and m.get("telegram"):
            return str(m["telegram"])
    # Fallback: first member with a telegram id at all
    for m in aaka_config.members():
        if m.get("telegram"):
            return str(m["telegram"])
    return None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_sender(from_addr: str) -> str:
    """Extract 'foo@bar.com' from 'Name <foo@bar.com>' or 'foo@bar.com'."""
    if not from_addr:
        return ""
    m = re.search(r"<([^>]+)>", from_addr)
    addr = (m.group(1) if m else from_addr).strip().lower()
    return addr if "@" in addr else ""


def _sender_domain(sender: str) -> str:
    return sender.split("@", 1)[1] if "@" in sender else ""


# ── Decision store ─────────────────────────────────────────────────────────────

def load_decisions(member_id: str) -> dict[str, dict]:
    """sender_email → {label, archive, action} for this member."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sender_email, label, "
            "  COALESCE(archive, 1) AS archive, "
            "  COALESCE(action, 'label') AS action "
            "FROM gmail_sender_decisions WHERE member_id=?",
            (member_id,),
        ).fetchall()
    return {
        r["sender_email"]: {
            "label": r["label"],
            "archive": bool(r["archive"]),
            "action": r["action"],
        }
        for r in rows
    }


_DRY_RUN = False  # set by main() so persistence helpers can short-circuit
_NO_ARCHIVE = False  # --no-archive flag: force keep-in-INBOX for all decisions
_NEVER_ARCHIVE_LABELS: set[str] = set()  # populated from rules YAML in main()

# Token usage tracking (rough chars/4 estimate — Anthropic doesn't return counts via CLI)
TOKEN_USAGE = {"prompt_chars": 0, "response_chars": 0, "calls": 0}


def _track_llm(prompt: str, response: str) -> None:
    TOKEN_USAGE["prompt_chars"] += len(prompt)
    TOKEN_USAGE["response_chars"] += len(response)
    TOKEN_USAGE["calls"] += 1


def _token_summary() -> str:
    in_tok = TOKEN_USAGE["prompt_chars"] // 4
    out_tok = TOKEN_USAGE["response_chars"] // 4
    # Haiku 4.5: $1/MTok input, $5/MTok output (rough)
    cost = (in_tok / 1_000_000) * 1.0 + (out_tok / 1_000_000) * 5.0
    return (f"LLM: {TOKEN_USAGE['calls']} calls, "
            f"~{in_tok:,} input + ~{out_tok:,} output tokens "
            f"(~${cost:.4f} on Haiku 4.5)")


def save_decision(member_id: str, sender: str, label: str, source: str,
                  archive: bool = True, action: str = "label") -> None:
    if _DRY_RUN:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO gmail_sender_decisions "
            "(member_id, sender_email, label, decided_at, source, archive, action) "
            "VALUES (?,?,?,?,?,?,?)",
            (member_id, sender, label, _now(), source, 1 if archive else 0, action),
        )
        conn.commit()


def bump_sender_stat(member_id: str, sender: str, label: str, unsub: str) -> None:
    if _DRY_RUN:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT INTO gmail_sender_stats "
            "(member_id, sender_email, msg_count, last_label, unsub_url, last_seen_at) "
            "VALUES (?,?,1,?,?,?) "
            "ON CONFLICT(member_id, sender_email) DO UPDATE SET "
            "  msg_count = msg_count + 1, "
            "  last_label = excluded.last_label, "
            "  unsub_url = COALESCE(NULLIF(excluded.unsub_url,''), unsub_url), "
            "  last_seen_at = excluded.last_seen_at",
            (member_id, sender, label, unsub, _now()),
        )
        conn.commit()


def save_progress(member_id: str, last_id: str, count: int) -> None:
    if _DRY_RUN:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT INTO gmail_sort_progress (member_id, last_message_id, processed_count, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(member_id) DO UPDATE SET "
            "  last_message_id = excluded.last_message_id, "
            "  processed_count = processed_count + excluded.processed_count, "
            "  updated_at = excluded.updated_at",
            (member_id, last_id, count, _now()),
        )
        conn.commit()


# ── Rules ──────────────────────────────────────────────────────────────────────

def load_rules(member_id: str = "") -> list[dict]:
    """Rules list for a member: per-member rules FIRST, then global rules.
    Per-member rules win because they're evaluated first.
    """
    data = _load_rules_file()
    member_block = (data.get("members") or {}).get(member_id) or {}
    return list(member_block.get("rules") or []) + list(data.get("rules") or [])


def load_watch_keywords(member_id: str = "") -> list[str]:
    """Watch keywords. Per-member REPLACES global (use to narrow).
    Falls back to global if member has no override.
    """
    data = _load_rules_file()
    member_block = (data.get("members") or {}).get(member_id) or {}
    raw = member_block.get("watch_keywords") or data.get("watch_keywords") or []
    seen, out = set(), []
    for k in raw:
        if not k:
            continue
        kw = str(k).lower()
        if kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out


def load_member_taxonomy(member_id: str) -> list[str] | None:
    """Per-member taxonomy override. None = use DEFAULT_TAXONOMY."""
    data = _load_rules_file()
    member_block = (data.get("members") or {}).get(member_id) or {}
    tax = member_block.get("taxonomy")
    return [str(t) for t in tax] if tax else None


def _load_rules_file() -> dict:
    if not RULES_FILE.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(RULES_FILE.read_text()) or {}
    except Exception as exc:
        print(f"{_R}rules load failed: {exc}{_N}", file=sys.stderr)
        return {}


def load_never_archive_labels(member_id: str = "") -> set[str]:
    """Labels that must never be auto-archived. Merges global + per-member.

    Exact-match against the label name (no substring). Use to protect
    transactional / financial categories from being hidden out of INBOX.
    """
    data = _load_rules_file()
    out = set(data.get("never_archive_labels") or [])
    if member_id:
        mb = (data.get("members") or {}).get(member_id) or {}
        out.update(mb.get("never_archive_labels") or [])
    return {str(s) for s in out if s}


def match_watch(msg_view: dict, watch_keywords: list[str]) -> bool:
    """True if any watch_keyword appears in sender/subject/snippet."""
    if not watch_keywords:
        return False
    hay = " ".join([
        msg_view.get("sender", ""),
        msg_view.get("subject", ""),
        msg_view.get("snippet", ""),
        msg_view.get("body", ""),
    ]).lower()
    return any(kw in hay for kw in watch_keywords)


def match_rule(msg_view: dict, rules: list[dict]) -> dict | None:
    """Return {label, archive, action} of the first matching rule, or None.

    msg_view: {sender, subject, snippet, body?}
    """
    sender = msg_view.get("sender", "")
    domain = _sender_domain(sender)
    subj = (msg_view.get("subject") or "").lower()
    for rule in rules:
        keywords = rule.get("keywords") or []
        if not keywords:
            continue
        scope = (rule.get("match") or "subject_or_body").lower()
        cs = bool(rule.get("case_sensitive"))
        for kw in keywords:
            needle = kw if cs else kw.lower()
            haystack = ""
            if scope == "sender_domain":
                haystack = sender if cs else sender.lower()
                if needle in haystack or needle.lstrip("@") in domain:
                    return _rule_result(rule)
                continue
            if scope == "subject_only":
                haystack = (msg_view.get("subject") or "") if cs else subj
            else:
                haystack = (
                    (msg_view.get("subject") or "") + " " +
                    (msg_view.get("snippet") or "") + " " +
                    (msg_view.get("body") or "")
                )
                if not cs:
                    haystack = haystack.lower()
            if needle in haystack:
                return _rule_result(rule)
    return None


def _rule_result(rule: dict) -> dict:
    """Resolve a rule to (label, archive, action), applying suffix conventions.

    Suffixes on the label name (kept in the Gmail label too):
      `*`  → pin to INBOX (archive=False)
      `~`  → retention-eligible (script may apply _TrashCandidate after N days
             in a future sweep; NEVER_TRASH still applies)
    Explicit `archive:`/`action:` in the rule wins over the suffix default.
    """
    label = rule.get("label", "")
    archive_default, _retention = _parse_label_suffixes(label)
    return {
        "label": label,
        "archive": bool(rule.get("archive", archive_default)),
        "action": (rule.get("action") or "label").lower(),  # 'label' | 'trash'
    }


def _parse_label_suffixes(label_name: str) -> tuple[bool, bool]:
    """Return (archive_default, retention_eligible) from label-name suffixes.

    `~` ⇒ retention-eligible; `*` ⇒ pin to INBOX (archive=False).
    The suffixes are kept on the Gmail label name — they are visible to the
    user so the convention is self-documenting in the Gmail UI.
    """
    # Strip and inspect just the trailing chars
    archive = True
    retention = False
    tail = label_name[-3:]
    if "*" in tail:
        archive = False
    if "~" in tail:
        retention = True
    return archive, retention


# ── Gmail label resolution ─────────────────────────────────────────────────────

def get_or_create_label_map(svc, wanted: list[str], dry_run: bool = False) -> dict[str, str]:
    """Return {label_name: label_id}, creating user labels that don't yet exist.

    In dry_run mode, missing labels get a placeholder id and are not created.
    """
    existing = {l["name"]: l["id"] for l in list_labels(svc)}
    out = {}
    for name in wanted:
        if name in existing:
            out[name] = existing[name]
            continue
        if dry_run:
            out[name] = f"<would-create:{name}>"
            print(f"  {_D}DRY +label{_N} {name}")
            continue
        try:
            created = create_label(svc, name)
            out[name] = created["id"]
            print(f"  {_C}+label{_N} {name} (id={created['id']})")
        except Exception as exc:
            print(f"  {_R}label create failed{_N} {name}: {exc}", file=sys.stderr)
    return out


def existing_user_labels(svc) -> list[str]:
    return sorted(l["name"] for l in list_labels(svc) if l.get("type") == "user")


# ── LLM classification ────────────────────────────────────────────────────────

def classify_batch(client: AakaClient, msgs: list[dict], taxonomy: list[str]) -> list[dict]:
    """Batched Haiku call. Returns list aligned with msgs:
       [{id, label, confidence, propose_new?}].
    """
    if not msgs:
        return []
    items = []
    for m in msgs:
        items.append({
            "id": m["id"],
            "from": m.get("from_addr", "")[:120],
            "subject": (m.get("subject") or "")[:200],
            "snippet": (m.get("snippet") or m.get("body", ""))[:500],
        })
    prompt = (
        "You are sorting Gmail messages. For each item, pick the single best label "
        f"from this set:\n  {', '.join(taxonomy)}\n\n"
        "If none fits, set label to one new short proposal (1-2 words, TitleCase) and "
        "set propose_new: true. Confidence is 0.0-1.0.\n\n"
        "Return ONLY a JSON array, one entry per item, in the same order:\n"
        '[{"id":"<id>","label":"<name>","confidence":<float>,"propose_new":<bool>}]\n\n'
        f"Items:\n{json.dumps(items, ensure_ascii=False)}"
    )
    try:
        raw = client.llm(prompt, response_format="json", complexity="low")
        _track_llm(prompt, raw)
    except AakaClientError as exc:
        print(f"  {_R}LLM call failed{_N}: {exc}", file=sys.stderr)
        return [{"id": m["id"], "label": "", "confidence": 0.0} for m in msgs]
    # Strip code fences if any
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last-ditch: find first [...] block
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        print(f"  {_R}LLM JSON parse failed{_N}; raw={raw[:200]}", file=sys.stderr)
        return [{"id": x["id"], "label": "", "confidence": 0.0} for x in msgs]


# ── Ask user (background thread) ──────────────────────────────────────────────

class AskTracker:
    """Tracks in-flight asks per sender so we don't ask twice."""

    def __init__(self, client: AakaClient, taxonomy: list[str], member_id: str,
                 telegram_sender: str | None):
        self.client = client
        self.taxonomy = taxonomy
        self.member_id = member_id
        self.telegram_sender = telegram_sender
        self._lock = threading.Lock()
        self._pending: dict[str, threading.Thread] = {}  # sender → thread
        self._answered: dict[str, str] = {}              # sender → label

    def is_pending(self, sender: str) -> bool:
        with self._lock:
            return sender in self._pending

    def fire(self, sender: str, sample_subject: str) -> None:
        with self._lock:
            if sender in self._pending or sender in self._answered:
                return
            t = threading.Thread(
                target=self._worker,
                args=(sender, sample_subject),
                daemon=True,
            )
            self._pending[sender] = t
        t.start()

    def _worker(self, sender: str, sample_subject: str) -> None:
        try:
            # Limit options to 6 for inline keyboard sanity
            opts = self.taxonomy[:6] + ["Trash", "Skip"]
            text = (
                f"📥 Label for **{sender}**?\n"
                f"_{sample_subject[:120]}_"
            )
            reply = self.client.ask(
                text,
                options=opts,
                sender=self.telegram_sender,
                timeout_minutes=720,
                poll_interval=8,
            )
            reply = (reply or "").strip()
            if reply.lower() in ("skip", ""):
                # Skip recorded as 'Skip' so we don't keep asking
                reply = "Skip"
            label = reply
            save_decision(self.member_id, sender, label, "ask")
            with self._lock:
                self._answered[sender] = label
                self._pending.pop(sender, None)
            print(f"  {_G}✓ ask{_N} {sender} → {label}")
        except Exception as exc:
            with self._lock:
                self._pending.pop(sender, None)
            print(f"  {_R}ask failed{_N} {sender}: {exc}", file=sys.stderr)

    def consume_answers(self) -> dict[str, str]:
        """Return newly-answered senders since last call; clears the buffer."""
        with self._lock:
            out = dict(self._answered)
            self._answered.clear()
            return out

    def wait_all(self, timeout_seconds: int = 7200) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            with self._lock:
                pending = list(self._pending.values())
            if not pending:
                return
            for t in pending:
                t.join(timeout=5)


# ── Apply + unsub extraction ──────────────────────────────────────────────────

def apply_decision(
    svc, member_id: str, msg_id: str, sender: str, label_name: str,
    label_ids: dict[str, str], dry_run: bool, archive: bool = True,
    action: str = "label",
) -> str:
    """Apply label + (optionally) archive; or trash. Returns 'ok' | 'skip' | 'noop'."""
    if label_name == "Skip":
        return "skip"

    # Watch label always stays in INBOX
    if label_name == WATCH_LABEL:
        archive = False
    # --no-archive run-wide override (highest precedence after action=trash)
    if _NO_ARCHIVE:
        archive = False
    # Never-archive label list from config (financial, transactional, etc.)
    if label_name in _NEVER_ARCHIVE_LABELS:
        archive = False

    if action == "trash":
        # HARD SAFETY: trash is forbidden — relabel as _TrashCandidate, archive,
        # let the user trash manually after review.
        if NEVER_TRASH:
            label_name = TRASH_CANDIDATE_LABEL
            archive = True
            action = "label"
            # fall through to the normal label path below
        else:
            if dry_run:
                print(f"  {_D}DRY{_N} {msg_id[:12]} {sender:<40} → {_R}TRASH{_N}")
                return "ok"
            try:
                apply_labels(svc, msg_id, add_ids=["TRASH"], remove_ids=["INBOX"])
                time.sleep(RATE_SLEEP)
                return "ok"
            except Exception as exc:
                print(f"  {_R}trash failed{_N} {msg_id[:12]}: {exc}", file=sys.stderr)
                return "noop"

    label_id = label_ids.get(label_name)
    if not label_id:
        if dry_run:
            label_ids[label_name] = f"<would-create:{label_name}>"
            label_id = label_ids[label_name]
        else:
            try:
                new = create_label(svc, label_name)
                label_id = new["id"]
                label_ids[label_name] = label_id
                print(f"  {_C}+label{_N} {label_name}")
            except Exception as exc:
                print(f"  {_R}create label failed{_N} {label_name}: {exc}", file=sys.stderr)
                return "noop"

    if dry_run:
        tag = f"{label_name}{' (keep)' if not archive else ''}"
        print(f"  {_D}DRY{_N} {msg_id[:12]} {sender:<40} → {tag}")
        return "ok"

    remove = ["INBOX"] if archive else None
    try:
        apply_labels(svc, msg_id, add_ids=[label_id], remove_ids=remove)
        time.sleep(RATE_SLEEP)
        return "ok"
    except Exception as exc:
        print(f"  {_R}apply failed{_N} {msg_id[:12]}: {exc}", file=sys.stderr)
        return "noop"


def write_unsub(member_id: str, sender: str, label: str, unsub_url: str) -> None:
    if _DRY_RUN or not unsub_url:
        return
    UNSUB_DIR.mkdir(parents=True, exist_ok=True)
    path = UNSUB_DIR / f"unsub_{member_id}.md"
    # Idempotent append: skip if line already present
    line = f"- [{label}] **{sender}** — {unsub_url}\n"
    if path.exists() and line in path.read_text():
        return
    with path.open("a") as f:
        if path.stat().st_size == 0:
            f.write(f"# Unsubscribe candidates ({member_id})\n\n")
        f.write(line)


# ── Inbox stream ──────────────────────────────────────────────────────────────

def stream_inbox(svc, max_total: int = 0, query: str = "in:inbox"):
    """Yield message ids matching a Gmail query. max_total=0 = unlimited."""
    page_token = None
    yielded = 0
    while True:
        kwargs = dict(userId="me", q=query, maxResults=BATCH_SIZE)
        if page_token:
            kwargs["pageToken"] = page_token
        result = svc.users().messages().list(**kwargs).execute()
        msgs = result.get("messages", []) or []
        if not msgs:
            return
        for m in msgs:
            if max_total and yielded >= max_total:
                return
            yield m["id"]
            yielded += 1
        page_token = result.get("nextPageToken")
        if not page_token:
            return


def hydrate(svc, msg_id: str, need_body: bool) -> dict:
    """Get headers + (optionally) body for a message."""
    if need_body:
        full = fetch_full(svc, msg_id, max_chars=1500)
        meta = fetch_headers_full(svc, msg_id)  # for List-Unsubscribe
        full["headers"] = meta["headers"]
        full["snippet"] = meta["snippet"]
        full["id"] = msg_id
        return full
    meta = fetch_headers_full(svc, msg_id)
    h = meta["headers"]
    return {
        "id": msg_id,
        "from_addr": h.get("From", ""),
        "subject": h.get("Subject", "(no subject)"),
        "date": h.get("Date", ""),
        "snippet": meta["snippet"],
        "body": "",
        "headers": h,
    }


# ── Main per-member pass ──────────────────────────────────────────────────────

def sort_member(
    member: dict, client: AakaClient, args: argparse.Namespace,
) -> None:
    member_id = member["id"]
    print(f"\n{_B}━━ {member.get('name', member_id)} <{member.get('email','')}>{_N}")

    try:
        svc = get_gmail_service(member_id)
    except Exception as exc:
        print(f"{_R}Gmail auth failed for {member_id}: {exc}{_N}", file=sys.stderr)
        return

    # Verify modify scope is present on the token
    try:
        creds = svc._http.credentials  # type: ignore[attr-defined]
        scopes = set(creds.scopes or [])
        if "https://www.googleapis.com/auth/gmail.modify" not in scopes:
            print(f"{_Y}⚠ token for {member_id} lacks gmail.modify scope.{_N}")
            print(f"  Run: python3 admin/reauth.py {member_id}")
            if not args.dry_run:
                print(f"  {_D}(skipping member — use --dry-run to preview without writes){_N}")
                return
    except Exception:
        pass

    # Taxonomy: per-member override, else default seed
    member_tax = load_member_taxonomy(member_id)
    seed_tax = member_tax if member_tax else DEFAULT_TAXONOMY
    user_labels = existing_user_labels(svc)
    taxonomy = sorted(set(user_labels) | set(seed_tax) | {WATCH_LABEL})
    taxonomy_for_llm = (seed_tax + user_labels)[:40]
    seed_origin = "per-member" if member_tax else "default"
    print(f"  Taxonomy: {len(taxonomy)} labels "
          f"({len(seed_tax)} seed [{seed_origin}] + {len(user_labels)} existing)")

    # Always ensure Watch + seed exist
    label_ids = get_or_create_label_map(svc, seed_tax + [WATCH_LABEL],
                                        dry_run=args.dry_run)
    # Mix in existing user labels
    for l in list_labels(svc):
        label_ids.setdefault(l["name"], l["id"])

    rules = load_rules(member_id)
    watch_keywords = load_watch_keywords(member_id)
    if rules or watch_keywords:
        print(f"  Rules: {len(rules)} rule(s), {len(watch_keywords)} watch keyword(s)")

    decisions = load_decisions(member_id)
    # Asks always go to the admin (PG), not to the target member —
    # the family member whose mail we're sorting may not be the operator.
    admin_tg = _admin_telegram()
    asker = AskTracker(
        client, taxonomy_for_llm, member_id,
        telegram_sender=admin_tg,
    )

    stats = {"rule": 0, "cached": 0, "llm": 0, "asked": 0, "deferred": 0, "applied": 0, "watch": 0}
    deferred: list[str] = []
    pending_llm: list[dict] = []
    pending_meta: dict[str, dict] = {}  # id → hydrated view (for replay)

    def flush_llm():
        if not pending_llm:
            return
        results = classify_batch(client, pending_llm, taxonomy_for_llm)
        results_by_id = {r["id"]: r for r in results if isinstance(r, dict)}
        for m in pending_llm:
            r = results_by_id.get(m["id"], {})
            label = (r.get("label") or "").strip()
            conf = float(r.get("confidence") or 0.0)
            sender = m["sender"]
            propose_new = bool(r.get("propose_new"))
            if not label or (conf < CONFIDENCE_THRESHOLD) or (propose_new and conf < 0.9):
                # Defer + ask
                if not args.no_ask:
                    asker.fire(sender, m.get("subject", ""))
                    deferred.append(m["id"])
                    stats["deferred"] += 1
                else:
                    print(f"  {_Y}?{_N} {sender:<40} (no-ask) skipped")
                continue
            save_decision(member_id, sender, label, "llm")
            decisions[sender] = {"label": label, "archive": True, "action": "label"}
            stats["llm"] += 1
            res = apply_decision(svc, member_id, m["id"], sender, label,
                                 label_ids, args.dry_run)
            if res == "ok":
                stats["applied"] += 1
                bump_sender_stat(member_id, sender, label,
                                 parse_list_unsubscribe(m.get("headers", {})))
                if label in PROMO_LIKE:
                    write_unsub(member_id, sender,
                                label, parse_list_unsubscribe(m.get("headers", {})))
        pending_llm.clear()

    seen_ids: set[str] = set()
    iter_ids = stream_inbox(svc, max_total=args.max)
    processed = 0
    for msg_id in iter_ids:
        if msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)

        # Cheap header pull first
        view = hydrate(svc, msg_id, need_body=False)
        sender = _parse_sender(view["from_addr"])
        if not sender:
            continue

        # 1. Already decided
        if sender in decisions:
            d = decisions[sender]
            label = d["label"]
            stats["cached"] += 1
            res = apply_decision(svc, member_id, msg_id, sender, label,
                                 label_ids, args.dry_run,
                                 archive=d["archive"], action=d["action"])
            if res == "ok":
                stats["applied"] += 1
                bump_sender_stat(member_id, sender, label,
                                 parse_list_unsubscribe(view["headers"]))
                if label in PROMO_LIKE or d["action"] == "trash":
                    write_unsub(member_id, sender, label,
                                parse_list_unsubscribe(view["headers"]))
            processed += 1
            continue

        # 2. Currently asking → defer
        if asker.is_pending(sender):
            deferred.append(msg_id)
            pending_meta[msg_id] = view
            continue

        # Consume any freshly-answered asks before deciding
        for s, lbl in asker.consume_answers().items():
            decisions[s] = {"label": lbl, "archive": True, "action": "label"}
        if sender in decisions:
            d = decisions[sender]
            label = d["label"]
            stats["cached"] += 1
            res = apply_decision(svc, member_id, msg_id, sender, label,
                                 label_ids, args.dry_run,
                                 archive=d["archive"], action=d["action"])
            if res == "ok":
                stats["applied"] += 1
                bump_sender_stat(member_id, sender, label,
                                 parse_list_unsubscribe(view["headers"]))
            processed += 1
            continue

        rule_view = {
            "sender": sender,
            "subject": view["subject"],
            "snippet": view["snippet"],
        }

        # 3a. Rule match (cheap, deterministic — runs FIRST so specific senders
        # like passport.admin@... win over generic watch keywords)
        rule = match_rule(rule_view, rules)
        if rule:
            save_decision(member_id, sender, rule["label"], "rule",
                          archive=rule["archive"], action=rule["action"])
            decisions[sender] = {
                "label": rule["label"],
                "archive": rule["archive"],
                "action": rule["action"],
            }
            stats["rule"] += 1
            res = apply_decision(svc, member_id, msg_id, sender, rule["label"],
                                 label_ids, args.dry_run,
                                 archive=rule["archive"], action=rule["action"])
            if res == "ok":
                stats["applied"] += 1
                bump_sender_stat(member_id, sender, rule["label"],
                                 parse_list_unsubscribe(view["headers"]))
                if rule["label"] in PROMO_LIKE or rule["action"] == "trash":
                    write_unsub(member_id, sender, rule["label"],
                                parse_list_unsubscribe(view["headers"]))
            processed += 1
            continue

        # 3b. Watch keyword (fallback — stays in INBOX, never archived)
        if match_watch(rule_view, watch_keywords):
            save_decision(member_id, sender, WATCH_LABEL, "watch", archive=False)
            decisions[sender] = {"label": WATCH_LABEL, "archive": False, "action": "label"}
            stats["watch"] += 1
            res = apply_decision(svc, member_id, msg_id, sender, WATCH_LABEL,
                                 label_ids, args.dry_run, archive=False)
            if res == "ok":
                stats["applied"] += 1
                bump_sender_stat(member_id, sender, WATCH_LABEL, "")
            processed += 1
            continue

        # 4. Queue for batched LLM call (need a bit more body)
        full_view = hydrate(svc, msg_id, need_body=True)
        pending_llm.append({
            "id": msg_id,
            "from_addr": full_view["from_addr"],
            "subject": full_view["subject"],
            "snippet": full_view["snippet"][:300],
            "body": full_view["body"],
            "sender": sender,
            "headers": full_view["headers"],
        })
        pending_meta[msg_id] = full_view
        if len(pending_llm) >= LLM_BATCH:
            flush_llm()

        processed += 1
        if processed % 50 == 0:
            save_progress(member_id, msg_id, 50)

    flush_llm()

    # Wait for all asks; replay deferred
    if deferred:
        print(f"\n  {_Y}Waiting on {len([s for s in {_parse_sender(pending_meta[d]['from_addr']) for d in deferred if d in pending_meta}])} senders…{_N}")
        asker.wait_all()
        for s, lbl in asker.consume_answers().items():
            decisions[s] = {"label": lbl, "archive": True, "action": "label"}

        replay_misses = 0
        for msg_id in deferred:
            view = pending_meta.get(msg_id) or hydrate(svc, msg_id, need_body=False)
            sender = _parse_sender(view["from_addr"])
            d = decisions.get(sender)
            if not d or d["label"] == "Skip":
                replay_misses += 1
                continue
            label = d["label"]
            res = apply_decision(svc, member_id, msg_id, sender, label,
                                 label_ids, args.dry_run,
                                 archive=d["archive"], action=d["action"])
            if res == "ok":
                stats["applied"] += 1
                stats["asked"] += 1
                bump_sender_stat(member_id, sender, label,
                                 parse_list_unsubscribe(view.get("headers", {})))
                if label in PROMO_LIKE:
                    write_unsub(member_id, sender, label,
                                parse_list_unsubscribe(view.get("headers", {})))
        if replay_misses:
            print(f"  {_D}{replay_misses} deferred message(s) had no decision (Skip / timeout){_N}")

    save_progress(member_id, "", 0)

    print(
        f"\n  {_B}Done{_N}: processed={processed} applied={stats['applied']} "
        f"rule={stats['rule']} watch={stats['watch']} cached={stats['cached']} "
        f"llm={stats['llm']} asked={stats['asked']} deferred={stats['deferred']}"
    )
    print(f"  {_D}{_token_summary()}{_N}")


# ── Anomaly detection (Haiku) ─────────────────────────────────────────────────

def _detect_anomalies(client: AakaClient, senders: dict, scope: str) -> list[dict]:
    """Ask Haiku which senders look out of place in this label.

    Returns [{sender, reason}].
    """
    items = sorted(
        ({"sender": s, "count": i["count"],
          "subject": (i["samples"][0][0] if i["samples"] else "")[:120]}
         for s, i in senders.items()),
        key=lambda x: -x["count"],
    )[:120]  # cap context

    prompt = (
        f"You're auditing a Gmail folder ({scope}). The user subscribes to "
        f"recurring newsletters / daily digests / weekly roundups here. Look at "
        f"this sender list (sorted by frequency, with the most recent subject) "
        f"and flag any sender that DOESN'T look like a subscribed newsletter — "
        f"e.g. a personal-looking address mixed in, a transactional notification, "
        f"a one-off receipt, a sender whose subject doesn't match a digest pattern, "
        f"or anything else that looks misfiled.\n\n"
        f"Return ONLY a JSON array (may be empty):\n"
        f'[{{"sender":"<email>","reason":"<short why>"}}]\n\n'
        f"Senders:\n{json.dumps(items, ensure_ascii=False)}"
    )
    try:
        raw = client.llm(prompt, response_format="json", complexity="low")
        _track_llm(prompt, raw)
    except AakaClientError as exc:
        print(f"  {_R}anomaly LLM call failed{_N}: {exc}", file=sys.stderr)
        return []
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return []


# ── --survey ──────────────────────────────────────────────────────────────────

def survey_member(member: dict, max_msgs: int, label: str = "",
                  client: AakaClient | None = None) -> None:
    """Enumerate distinct senders for a member.

    If `label` is empty, surveys INBOX (default). Otherwise surveys that Gmail
    label (label:NAME). When `client` is provided, also asks Haiku to flag
    anomalous senders (frequency / domain / naming outliers).

    Writes a markdown report to $AAKA_CONFIG_DIR/data/gmail_sort/survey_<id>[_<label>].md
    """
    member_id = member["id"]
    scope = f"label `{label}`" if label else "INBOX"
    print(f"\n{_B}━━ Survey: {member.get('name', member_id)} — {scope}{_N}")

    try:
        svc = get_gmail_service(member_id)
    except Exception as exc:
        print(f"{_R}Gmail auth failed: {exc}{_N}", file=sys.stderr)
        return

    watch_keywords = load_watch_keywords(member_id)

    query = f"label:{label}" if label else "in:inbox"
    msg_ids = list(stream_inbox(svc, max_total=max_msgs, query=query))
    print(f"  Scanned: {len(msg_ids)} message ids; pulling headers…")

    # Sequential header pulls (googleapiclient http isn't thread-safe per service).
    # Progress every 100 msgs.
    senders: dict[str, dict] = {}
    for i, mid in enumerate(msg_ids, 1):
        try:
            meta = fetch_headers_full(svc, mid)
        except Exception as exc:
            print(f"  {_R}header pull failed{_N} {mid[:10]}: {exc}", file=sys.stderr)
            continue
        h = meta["headers"]
        sender = _parse_sender(h.get("From", ""))
        if not sender:
            continue
        entry = senders.setdefault(sender, {
            "count": 0,
            "samples": [],
            "unsub": "",
            "watch_hits": 0,
        })
        entry["count"] += 1
        if len(entry["samples"]) < 3:
            entry["samples"].append((h.get("Subject", "(no subject)"), meta["snippet"][:120]))
        if not entry["unsub"]:
            entry["unsub"] = parse_list_unsubscribe(h)
        blob = (sender + " " + h.get("Subject", "") + " " + meta["snippet"]).lower()
        if watch_keywords and any(kw in blob for kw in watch_keywords):
            entry["watch_hits"] += 1
        if i % 100 == 0:
            print(f"  …{i}/{len(msg_ids)} headers ({len(senders)} senders)")

    print(f"  Distinct senders: {len(senders)}")

    # Bucket
    unsub_bucket: list[tuple] = []
    archive_bucket: list[tuple] = []
    watch_bucket: list[tuple] = []
    other_bucket: list[tuple] = []

    for sender, info in senders.items():
        if info["watch_hits"] > 0:
            watch_bucket.append((sender, info))
        elif info["unsub"] and info["count"] >= 2:
            unsub_bucket.append((sender, info))
        elif info["count"] >= 3:
            archive_bucket.append((sender, info))
        else:
            other_bucket.append((sender, info))

    for b in (unsub_bucket, archive_bucket, watch_bucket, other_bucket):
        b.sort(key=lambda x: -x[1]["count"])

    # Anomaly detection via Haiku (only if client given and we have ≥5 senders)
    anomalies: list[dict] = []
    if client is not None and len(senders) >= 5:
        anomalies = _detect_anomalies(client, senders, scope)

    UNSUB_DIR.mkdir(parents=True, exist_ok=True)
    fname_suffix = f"_{label.replace('/', '-')}" if label else ""
    report = UNSUB_DIR / f"survey_{member_id}{fname_suffix}.md"
    lines = [
        f"# Gmail sender survey — {member.get('name', member_id)} — {scope}",
        f"_Generated {_now()} from {len(msg_ids)} messages._",
        f"_{len(senders)} distinct senders._",
        "",
    ]
    if anomalies:
        lines += ["## ⚠ Anomalies (Haiku-flagged)", ""]
        lines.append("| Sender | Why it stands out |")
        lines.append("|---|---|")
        for a in anomalies:
            lines.append(f"| `{a.get('sender','')}` | {a.get('reason','')} |")
        lines.append("")
    lines += [
        "## Manual review (matched watch keywords)",
        "",
    ]
    if watch_bucket:
        lines.append("| Count | Sender | Hits | Sample |")
        lines.append("|---:|---|---:|---|")
        for s, i in watch_bucket:
            sample = i["samples"][0][0] if i["samples"] else ""
            lines.append(f"| {i['count']} | `{s}` | {i['watch_hits']} | {sample[:80]} |")
    else:
        lines.append("_(none)_")

    lines += ["", "## Unsubscribe candidates (bulk + List-Unsubscribe header)", ""]
    if unsub_bucket:
        lines.append("| Count | Sender | Unsubscribe |")
        lines.append("|---:|---|---|")
        for s, i in unsub_bucket:
            lines.append(f"| {i['count']} | `{s}` | {i['unsub']} |")
    else:
        lines.append("_(none)_")

    lines += ["", "## Archive candidates (bulk, no unsub)", ""]
    if archive_bucket:
        lines.append("| Count | Sender | Sample |")
        lines.append("|---:|---|---|")
        for s, i in archive_bucket:
            sample = i["samples"][0][0] if i["samples"] else ""
            lines.append(f"| {i['count']} | `{s}` | {sample[:80]} |")
    else:
        lines.append("_(none)_")

    lines += ["", "## Other (single / few messages)", ""]
    if other_bucket:
        lines.append("| Count | Sender |")
        lines.append("|---:|---|")
        for s, i in other_bucket[:80]:
            lines.append(f"| {i['count']} | `{s}` |")
        if len(other_bucket) > 80:
            lines.append(f"_…and {len(other_bucket) - 80} more_")
    else:
        lines.append("_(none)_")

    report.write_text("\n".join(lines) + "\n")
    print(f"  {_G}Wrote{_N} {report}")
    print(
        f"  Buckets — watch:{len(watch_bucket)} "
        f"unsub:{len(unsub_bucket)} archive:{len(archive_bucket)} "
        f"other:{len(other_bucket)}"
    )


# ── --rebind-from-rules ───────────────────────────────────────────────────────

def rebind_from_rules() -> None:
    """Re-evaluate rules against existing sender decisions; report what would change."""
    rules = load_rules()
    if not rules:
        print(f"{_Y}No rules loaded.{_N}")
        return
    with _connect() as conn:
        rows = conn.execute(
            "SELECT member_id, sender_email, label, source FROM gmail_sender_decisions"
        ).fetchall()
    changes = 0
    for r in rows:
        sender = r["sender_email"]
        # We don't have subject/body here — only sender_domain rules apply
        view = {"sender": sender, "subject": "", "snippet": ""}
        new = match_rule(view, rules)
        if new and new["label"] != r["label"]:
            print(f"  {sender:<40} {r['label']:>15} → {_C}{new['label']}{_N}")
            save_decision(r["member_id"], sender, new["label"], "rule",
                          archive=new["archive"], action=new["action"])
            changes += 1
    print(f"\n{changes} sender(s) rebound from rules.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--member", default="", help="Member id (default: all with gmail tokens)")
    parser.add_argument("--max", type=int, default=0, help="Max messages per member (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true", help="Show planned actions, no writes")
    parser.add_argument("--no-ask", action="store_true", help="Skip uncertain (do not message user)")
    parser.add_argument("--no-archive", action="store_true",
                        help="Safety: force keep-in-INBOX for every decision (label only).")
    parser.add_argument("--rebind-from-rules", action="store_true",
                        help="Re-evaluate sender_domain rules against existing decisions")
    parser.add_argument("--survey", action="store_true",
                        help="Enumerate distinct senders (no writes) and write a triage report")
    parser.add_argument("--label", default="",
                        help="Scope survey/sort to Gmail label (comma-separated for multiple)")
    args = parser.parse_args()

    global _DRY_RUN, _NO_ARCHIVE, _NEVER_ARCHIVE_LABELS
    _DRY_RUN = args.dry_run
    _NO_ARCHIVE = args.no_archive
    _NEVER_ARCHIVE_LABELS = load_never_archive_labels(args.member)
    if _NO_ARCHIVE:
        print(f"{_Y}--no-archive: every decision will keep INBOX{_N}")
    if _NEVER_ARCHIVE_LABELS:
        print(f"{_D}never-archive labels: {sorted(_NEVER_ARCHIVE_LABELS)}{_N}")

    if args.rebind_from_rules:
        rebind_from_rules()
        return

    # --survey: header-only pass; needs agent key only for anomaly detection
    if args.survey:
        if args.member:
            ms = [m for m in aaka_config.auth_members() if m["id"] == args.member]
        else:
            ms = [m for m in aaka_config.auth_members()
                  if any("gmail" in s for s in m.get("auth", {}).get("scopes", []))]
        if not ms:
            print(f"{_R}No matching members with gmail tokens.{_N}", file=sys.stderr)
            sys.exit(1)
        survey_client = None
        try:
            survey_client = AakaClient(api_key=_load_api_key()) if _load_api_key() else None
        except Exception:
            survey_client = None
        labels = [l.strip() for l in (args.label or "").split(",") if l.strip()] or [""]
        for m in ms:
            for lbl in labels:
                survey_member(m, max_msgs=args.max or 1000, label=lbl,
                              client=survey_client)
        print(f"\n{_B}Run total{_N} — {_token_summary()}")
        return

    api_key = _load_api_key()
    if not api_key:
        print(f"{_R}AAKA_AGENT_KEY not set. Register the agent:{_N}", file=sys.stderr)
        print(f"  python3 admin/register_agent.py gmail-sorter \"Gmail Sorter\" "
              f"--location /Users/Shared/aaka-repo/tools/gmail_sort.py", file=sys.stderr)
        sys.exit(1)

    client = AakaClient(api_key=api_key)

    # Resolve target members
    if args.member:
        m = next((x for x in aaka_config.auth_members() if x["id"] == args.member), None)
        if not m:
            print(f"{_R}Unknown member: {args.member}{_N}", file=sys.stderr)
            sys.exit(1)
        members_list = [m]
    else:
        members_list = [
            m for m in aaka_config.auth_members()
            if any("gmail" in s for s in m.get("auth", {}).get("scopes", []))
        ]
    if not members_list:
        print(f"{_R}No members with gmail scopes found.{_N}", file=sys.stderr)
        sys.exit(1)

    print(f"{_B}Gmail sort{_N}: {len(members_list)} member(s), "
          f"max={args.max or '∞'}, dry_run={args.dry_run}, no_ask={args.no_ask}")

    for member in members_list:
        try:
            sort_member(member, client, args)
        except KeyboardInterrupt:
            print(f"\n{_Y}Interrupted — progress saved.{_N}")
            print(f"  {_token_summary()}")
            sys.exit(130)

    print(f"\n{_B}Run total{_N} — {_token_summary()}")


if __name__ == "__main__":
    main()
