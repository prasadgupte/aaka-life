#!/usr/bin/env python3
"""
Standalone entry point for the mail fetch cron job.
Called by launchd every 15 minutes via com.aaka.mailfetch.plist.
"""

import logging
import os
import sys
from pathlib import Path

BASE = Path(
    os.environ.get("AAKA_BASE")
    or Path(__file__).resolve().parent.parent.parent
)
sys.path.insert(0, str(BASE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mail-fetch] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

from skills.mail.fetch import fetch_all_accounts

if __name__ == "__main__":
    result = fetch_all_accounts()
    total = result["total_fetched"]
    checked = result["accounts_checked"]
    print(f"[mail-fetch] Done: {total} new message(s) from {checked} account(s)")
    for r in result["results"]:
        status = f"{r['fetched']} new"
        if "error" in r:
            status += f" (error: {r['error']})"
        print(f"  {r['name']} ({r['protocol']}): {status}")
