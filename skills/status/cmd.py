#!/usr/bin/env python3
"""
CLI entry point for status sub-commands.

Usage:
    python3 skills/status/cmd.py [code|queue|llm]
"""

import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(BASE))

from skills.status.status_core import status_code, status_queue
from skills.status.llm import status_llm

_USAGE = "Usage: cmd.py [code|queue|llm]"

if __name__ == "__main__":
    sub = sys.argv[1] if len(sys.argv) > 1 else "help"
    if sub == "code":
        print(status_code())
    elif sub == "queue":
        print(status_queue())
    elif sub == "llm":
        print(status_llm())
    else:
        print(_USAGE)
        sys.exit(1 if sub not in ("help", "--help", "-h") else 0)
