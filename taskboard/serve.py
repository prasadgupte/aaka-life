#!/usr/bin/env python3
"""
taskboard/serve.py — Aaka Taskboard server.

Binds 127.0.0.1:8002 by default (personal tool). To expose it, aaka's portable
`webauth` guard applies: a public bind is REFUSED unless a page secret is set, and
when set every request is gated (see webauth.py). Behind your own auth proxy, leave
the secret unset — it binds localhost and the guard stays dormant.

Serves the SPA from static/ and the API from api.py.

Usage:
  python3 taskboard/serve.py [--port 8002] [--host 127.0.0.1]
  uvicorn taskboard.serve:app --port 8002
"""
import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from taskboard.api import router
import webauth

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Aaka Taskboard", version="1")

# Portable app-level guard: dormant on localhost / no secret; enforces a signed
# cookie (magic-link ?k=) when a page secret is configured. Survives proxy bypass.
webauth.install_guard(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8002"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("TASKBOARD_PORT", 8002)))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    # Anti-footgun: refuse a public bind unless a page secret is configured.
    webauth.assert_safe_bind(args.host)

    import uvicorn
    uvicorn.run("taskboard.serve:app", host=args.host, port=args.port, reload=False)
