"""
Password store — Fernet-encrypted local vault with macOS Keychain integration.

Vault file:  /Users/Shared/secrets/password-store/vault.enc
Salt file:   /Users/Shared/secrets/password-store/salt
Backups:     /Users/Shared/secrets/password-store/.backup/vault.enc.<timestamp>

Master password is stored in macOS Keychain under service name "aaka-password-store".
Set it once with: python3 tools/pw_setup.py

Usage:
    from tools.password_store import PasswordStore
    store = PasswordStore()
    store.search("gmail")          # returns list of {name, url, username, grouping}
    store.get_password("email/personal-pop")  # returns plaintext password (API use only)
"""

import base64
import csv
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

VAULT_DIR = Path("/Users/Shared/secrets/password-store")
VAULT_FILE = VAULT_DIR / "vault.enc"
SALT_FILE = VAULT_DIR / "salt"
BACKUP_DIR = VAULT_DIR / ".backup"

KEYCHAIN_SERVICE = "aaka-password-store"
PBKDF2_ITERATIONS = 600_000


def _get_master_from_keychain() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Master password not found in Keychain.\n"
            "Run: python3 tools/pw_setup.py"
        )
    return result.stdout.strip()


def _derive_key(master_password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    derived = kdf.derive(master_password.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


class PasswordStore:
    def __init__(self, vault_path: Path = None, master_password: str = None):
        self._vault_path = Path(vault_path) if vault_path else VAULT_FILE
        self._salt_path = self._vault_path.parent / "salt"
        self._master_password = master_password or _get_master_from_keychain()
        self._fernet = None  # lazy-init on first vault access

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            if not self._salt_path.exists():
                raise RuntimeError(
                    f"Salt file not found: {self._salt_path}\n"
                    "Run: python3 tools/pw_setup.py"
                )
            salt = self._salt_path.read_bytes()
            key = _derive_key(self._master_password, salt)
            self._fernet = Fernet(key)
        return self._fernet

    def _load_vault(self) -> list[dict]:
        if not self._vault_path.exists():
            raise RuntimeError(
                f"Vault not found: {self._vault_path}\n"
                "Run: python3 tools/pw_setup.py"
            )
        try:
            raw = self._vault_path.read_bytes()
            decrypted = self._get_fernet().decrypt(raw)
            data = json.loads(decrypted)
            return data.get("entries", [])
        except InvalidToken:
            raise RuntimeError("Wrong master password or corrupted vault.")

    def _save_vault(self, entries: list[dict]) -> None:
        self._vault_path.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        # Rotate backup before overwriting
        if self._vault_path.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            shutil.copy2(self._vault_path, BACKUP_DIR / f"vault.enc.{ts}")
            # Keep only last 20 backups
            backups = sorted(BACKUP_DIR.glob("vault.enc.*"))
            for old in backups[:-20]:
                old.unlink()

        data = {"version": 1, "entries": entries}
        plaintext = json.dumps(data, ensure_ascii=False, indent=None).encode("utf-8")
        encrypted = self._get_fernet().encrypt(plaintext)
        self._vault_path.write_bytes(encrypted)
        self._vault_path.chmod(0o600)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Read operations — safe for chat (no passwords returned)
    # ------------------------------------------------------------------

    def list_entries(self, group: str = None) -> list[dict]:
        """Return metadata only: name, url, username, grouping. No passwords."""
        entries = self._load_vault()
        if group:
            g = group.lower()
            entries = [e for e in entries if g in e.get("grouping", "").lower()]
        return [
            {
                "name": e["name"],
                "url": e.get("url", ""),
                "username": e.get("username", ""),
                "grouping": e.get("grouping", ""),
            }
            for e in entries
        ]

    def search(self, query: str) -> list[dict]:
        """Fuzzy match against name, url, grouping, username. No passwords returned."""
        q = query.lower()
        entries = self._load_vault()
        results = []
        for e in entries:
            haystack = " ".join([
                e.get("name", ""),
                e.get("url", ""),
                e.get("grouping", ""),
                e.get("username", ""),
            ]).lower()
            if q in haystack:
                results.append({
                    "name": e["name"],
                    "url": e.get("url", ""),
                    "username": e.get("username", ""),
                    "grouping": e.get("grouping", ""),
                })
        return results

    def count(self) -> int:
        return len(self._load_vault())

    # ------------------------------------------------------------------
    # Password retrieval — API use only, never expose to chat
    # ------------------------------------------------------------------

    def get_entry(self, name: str) -> dict:
        """Full entry including password. For programmatic use only."""
        entries = self._load_vault()
        for e in entries:
            if e["name"] == name:
                return e
        raise KeyError(f"No entry with name: {name!r}")

    def get_password(self, name: str) -> str:
        """Return password string for a named entry. API use only."""
        return self.get_entry(name)["password"]

    # ------------------------------------------------------------------
    # Write operations — local CLI only
    # ------------------------------------------------------------------

    def add_entry(
        self,
        name: str,
        username: str,
        password: str,
        url: str = "",
        grouping: str = "",
        extra: str = "",
    ) -> None:
        entries = self._load_vault()
        if any(e["name"] == name for e in entries):
            raise ValueError(f"Entry {name!r} already exists. Use update_entry() to modify.")
        entries.append({
            "name": name,
            "username": username,
            "password": password,
            "url": url,
            "grouping": grouping,
            "extra": extra,
            "created": self._now(),
            "modified": self._now(),
        })
        self._save_vault(entries)

    def update_entry(self, name: str, **fields) -> None:
        entries = self._load_vault()
        for e in entries:
            if e["name"] == name:
                allowed = {"username", "password", "url", "grouping", "extra", "name"}
                for k, v in fields.items():
                    if k in allowed:
                        e[k] = v
                e["modified"] = self._now()
                self._save_vault(entries)
                return
        raise KeyError(f"No entry with name: {name!r}")

    def delete_entry(self, name: str) -> None:
        entries = self._load_vault()
        before = len(entries)
        entries = [e for e in entries if e["name"] != name]
        if len(entries) == before:
            raise KeyError(f"No entry with name: {name!r}")
        self._save_vault(entries)

    # ------------------------------------------------------------------
    # Import / export
    # ------------------------------------------------------------------

    def import_lastpass_csv(self, csv_path: str, overwrite: bool = False) -> int:
        """
        Import a LastPass CSV export.
        Columns: url, username, password, extra, name, grouping, fav
        Returns number of entries imported.
        """
        entries = self._load_vault()
        existing_names = {e["name"] for e in entries}

        imported = 0
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("name", "").strip()
                if not name:
                    continue
                username = row.get("username", "").strip()
                password = row.get("password", "").strip()
                url = row.get("url", "").strip()
                grouping = row.get("grouping", "").strip()
                extra = row.get("extra", "").strip()

                if name in existing_names:
                    if overwrite:
                        for e in entries:
                            if e["name"] == name:
                                e.update({
                                    "username": username,
                                    "password": password,
                                    "url": url,
                                    "grouping": grouping,
                                    "extra": extra,
                                    "modified": self._now(),
                                })
                        imported += 1
                    # else skip duplicate
                else:
                    entries.append({
                        "name": name,
                        "username": username,
                        "password": password,
                        "url": url,
                        "grouping": grouping,
                        "extra": extra,
                        "created": self._now(),
                        "modified": self._now(),
                    })
                    existing_names.add(name)
                    imported += 1

        self._save_vault(entries)
        return imported

    def export_encrypted(self, dest_path: str) -> None:
        """Copy encrypted vault to dest_path (safe to back up)."""
        shutil.copy2(self._vault_path, dest_path)


# ------------------------------------------------------------------
# Vault initialization (used by pw_setup.py)
# ------------------------------------------------------------------

def init_vault(master_password: str, vault_path: Path = None, salt_path: Path = None) -> None:
    """Create a new empty vault with a freshly generated salt."""
    import secrets as _secrets
    vp = Path(vault_path) if vault_path else VAULT_FILE
    sp = Path(salt_path) if salt_path else SALT_FILE

    vp.parent.mkdir(parents=True, exist_ok=True)
    sp.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    salt = _secrets.token_bytes(16)
    sp.write_bytes(salt)
    sp.chmod(0o600)

    key = _derive_key(master_password, salt)
    f = Fernet(key)
    data = {"version": 1, "entries": []}
    encrypted = f.encrypt(json.dumps(data).encode("utf-8"))
    vp.write_bytes(encrypted)
    vp.chmod(0o600)
