"""
Mail fetch skill — POP3/IMAP fetch engine.

Fetches new mail from configured accounts and stores locally in Maildir format.
Credentials are resolved from the password store (tools/password_store.py).

Account config: $AAKA_SECRETS_ROOT/mail-fetch/accounts.yaml (default ~/.aaka/secrets/mail-fetch/accounts.yaml)

Usage (cron):
    python3 skills/mail/cron_fetch.py

Usage (aaka skill entrypoint):
    execute({"accounts": ["all"]})   # fetch all accounts
    execute({"accounts": ["pop3-work"]})  # fetch specific account
"""

import email as email_lib
import imaplib
import json
import logging
import mailbox
import os
import poplib
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

BASE = Path(
    os.environ.get("AAKA_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))
import aaka_config

log = logging.getLogger("mail.fetch")

ACCOUNTS_CONFIG = aaka_config.secrets_root() / "mail-fetch" / "accounts.yaml"
MAIL_DATA_DIR = aaka_config.DATA_DIR / "mail"


def _load_accounts() -> list[dict]:
    if not ACCOUNTS_CONFIG.exists():
        return []
    with open(ACCOUNTS_CONFIG) as f:
        data = yaml.safe_load(f)
    return [a for a in (data or {}).get("accounts", []) if a.get("enabled", True)]


def _resolve_password(account: dict) -> str:
    password_ref = account.get("password_ref")
    if password_ref:
        try:
            sys.path.insert(0, str(BASE))
            from tools.password_store import PasswordStore
            return PasswordStore().get_password(password_ref)
        except Exception as e:
            log.warning(f"Could not resolve password_ref {password_ref!r}: {e}")
    # Fallback: inline password field (not recommended)
    return account.get("password", "")


def _maildir_path(account_name: str) -> Path:
    return MAIL_DATA_DIR / account_name


def _seen_uids_path(account_name: str) -> Path:
    return _maildir_path(account_name) / ".seen_uids.json"


def _last_fetch_path(account_name: str) -> Path:
    return _maildir_path(account_name) / ".last_fetch.json"


def _load_seen_uids(account_name: str) -> set:
    p = _seen_uids_path(account_name)
    if not p.exists():
        return set()
    with open(p) as f:
        data = json.load(f)
    return set(data.keys())


def _save_seen_uids(account_name: str, uids: set) -> None:
    p = _seen_uids_path(account_name)
    # Load existing to merge
    if p.exists():
        with open(p) as f:
            existing = json.load(f)
    else:
        existing = {}
    ts = datetime.now(timezone.utc).isoformat()
    for uid in uids:
        if uid not in existing:
            existing[uid] = ts
    with open(p, "w") as f:
        json.dump(existing, f)


def _store_message(account_name: str, raw_bytes: bytes) -> None:
    mdir_path = _maildir_path(account_name)
    mdir_path.mkdir(parents=True, exist_ok=True)
    mdir = mailbox.Maildir(str(mdir_path), create=True)
    msg = email_lib.message_from_bytes(raw_bytes)
    mdir.add(msg)
    mdir.flush()


def _write_last_fetch(account_name: str, count: int, errors: list) -> None:
    p = _last_fetch_path(account_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "count": count,
        "errors": errors,
    }
    with open(p, "w") as f:
        json.dump(data, f)


def fetch_pop3(account: dict) -> int:
    """Fetch new messages from a POP3 account. Returns count of new messages fetched."""
    name = account["name"]
    host = account["host"]
    port = int(account.get("port", 995))
    username = account["username"]
    password = _resolve_password(account)
    fetch_limit = int(account.get("fetch_limit", 100))

    seen = _load_seen_uids(name)
    fetched = 0
    errors = []

    try:
        conn = poplib.POP3_SSL(host, port, timeout=30)
        conn.user(username)
        conn.pass_(password)

        # Get UIDL list (uid per message)
        resp, uidl_lines, _ = conn.uidl()
        # Each line: b"N uid-string"
        msg_uids = []
        for line in uidl_lines:
            parts = line.decode("utf-8", errors="replace").split(" ", 1)
            if len(parts) == 2:
                msg_num, uid = parts
                msg_uids.append((int(msg_num), uid.strip()))

        new_msgs = [(n, uid) for n, uid in msg_uids if uid not in seen]
        new_msgs = new_msgs[:fetch_limit]

        new_uids = set()
        for msg_num, uid in new_msgs:
            try:
                resp, lines, octets = conn.retr(msg_num)
                raw = b"\r\n".join(lines)
                _store_message(name, raw)
                new_uids.add(uid)
                fetched += 1
            except Exception as e:
                errors.append(str(e))
                log.warning(f"[{name}] POP3 retr msg {msg_num} failed: {e}")

        conn.quit()
        _save_seen_uids(name, new_uids)

    except Exception as e:
        errors.append(str(e))
        log.error(f"[{name}] POP3 connection failed: {e}")

    _write_last_fetch(name, fetched, errors)
    return fetched


def fetch_imap(account: dict) -> int:
    """Fetch new messages from an IMAP account. Returns count of new messages fetched."""
    name = account["name"]
    host = account["host"]
    port = int(account.get("port", 993))
    username = account["username"]
    password = _resolve_password(account)
    folders = account.get("folders", ["INBOX"])

    seen = _load_seen_uids(name)
    fetched = 0
    errors = []

    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(username, password)

        for folder in folders:
            try:
                status, _ = conn.select(folder)
                if status != "OK":
                    continue

                # Fetch all UIDs
                status, data = conn.uid("search", None, "ALL")
                if status != "OK" or not data[0]:
                    continue

                all_uids = data[0].split()
                new_uids_bytes = [u for u in all_uids if u.decode() not in seen]

                for uid_bytes in new_uids_bytes:
                    uid = uid_bytes.decode()
                    try:
                        status, msg_data = conn.uid("fetch", uid_bytes, "(RFC822)")
                        if status != "OK":
                            continue
                        raw = msg_data[0][1]
                        _store_message(name, raw)
                        seen.add(uid)
                        fetched += 1
                    except Exception as e:
                        errors.append(str(e))
                        log.warning(f"[{name}] IMAP fetch uid {uid} failed: {e}")

            except Exception as e:
                errors.append(str(e))
                log.warning(f"[{name}] IMAP folder {folder!r} failed: {e}")

        conn.logout()
        _save_seen_uids(name, seen)

    except Exception as e:
        errors.append(str(e))
        log.error(f"[{name}] IMAP connection failed: {e}")

    _write_last_fetch(name, fetched, errors)
    return fetched


def fetch_all_accounts(only: list = None) -> dict:
    """
    Fetch all enabled accounts.
    only: if set, list of account names to restrict to.
    Returns: {total_fetched, accounts_checked, results: [{name, fetched, protocol}]}
    """
    accounts = _load_accounts()
    if only and only != ["all"]:
        accounts = [a for a in accounts if a["name"] in only]

    total = 0
    results = []
    for account in accounts:
        name = account["name"]
        protocol = account.get("protocol", "pop3").lower()
        try:
            if protocol == "pop3":
                count = fetch_pop3(account)
            elif protocol == "imap":
                count = fetch_imap(account)
            else:
                log.warning(f"[{name}] Unknown protocol: {protocol}")
                count = 0
            total += count
            results.append({"name": name, "protocol": protocol, "fetched": count})
            log.info(f"[{name}] {protocol.upper()} fetched {count} new message(s)")
        except Exception as e:
            log.error(f"[{name}] Fetch failed: {e}")
            results.append({"name": name, "protocol": protocol, "fetched": 0, "error": str(e)})

    return {
        "total_fetched": total,
        "accounts_checked": len(accounts),
        "results": results,
    }


def execute(payload: dict) -> dict:
    """Aaka skill entrypoint."""
    only = payload.get("accounts", ["all"])
    return fetch_all_accounts(only=only)
