#!/usr/bin/env python3
"""
taskboard/serve.py — Aaka Taskboard server.

Localhost-only, port 8002. No auth — personal tool.
Serves the SPA from static/ and the API from api.py.

Usage:
  python3 taskboard/serve.py [--port 8002]
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

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Aaka Taskboard", version="1")

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

    import uvicorn
    uvicorn.run("taskboard.serve:app", host=args.host, port=args.port, reload=False)
