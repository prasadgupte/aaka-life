#!/usr/bin/env python3
"""
One-time password store setup.

Creates the encrypted vault and stores the master password in macOS Keychain.

Usage:
    python3 tools/pw_setup.py
    python3 tools/pw_setup.py --reset   # overwrite existing vault (keeps backups)
"""

import getpass
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.password_store import (
    KEYCHAIN_SERVICE,
    VAULT_FILE,
    SALT_FILE,
    init_vault,
)


def store_in_keychain(master_password: str) -> None:
    result = subprocess.run(
        [
            "security", "add-generic-password",
            "-s", KEYCHAIN_SERVICE,
            "-a", os.environ.get("USER", "aaka"),
            "-w", master_password,
            "-U",  # update if already exists
        ],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to store in Keychain: {result.stderr.strip()}")


def main():
    reset = "--reset" in sys.argv

    if VAULT_FILE.exists() and not reset:
        print(f"Vault already exists at {VAULT_FILE}")
        print("Use --reset to overwrite (existing backups are preserved).")
        sys.exit(0)

    print("Aaka Password Store — initial setup")
    print("=" * 40)
    print(f"Vault:  {VAULT_FILE}")
    print(f"Salt:   {SALT_FILE}")
    print()

    pw1 = getpass.getpass("Enter master password: ")
    pw2 = getpass.getpass("Confirm master password: ")
    if pw1 != pw2:
        print("Passwords do not match. Aborting.")
        sys.exit(1)
    if len(pw1) < 8:
        print("Master password must be at least 8 characters.")
        sys.exit(1)

    print("Creating vault...", end=" ", flush=True)
    init_vault(pw1)
    print("done.")

    print("Storing master password in macOS Keychain...", end=" ", flush=True)
    store_in_keychain(pw1)
    print("done.")

    print()
    print("Setup complete. Next steps:")
    print(f"  Import LastPass CSV:  python3 tools/pw_import.py /path/to/export.csv")
    print(f"  Add an entry:         python3 -c \"from tools.password_store import PasswordStore; "
          f"PasswordStore().add_entry('email/myaccount', 'user@example.com', 'mypassword', url='pop.example.com', grouping='email')\"")


if __name__ == "__main__":
    main()
