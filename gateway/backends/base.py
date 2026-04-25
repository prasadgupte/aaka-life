"""
gateway/backends/base.py — WhatsApp backend protocol.

Each backend (openclaw, hermes, …) must implement this protocol.
Select a backend by channel in aaka.yaml:
    channels:
      whatsapp:
        backend: openclaw   # or "hermes"
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class WhatsAppBackend(Protocol):
    """Contract every WhatsApp-capable backend must satisfy."""

    def send_text(
        self,
        target: str,
        text: str,
        *,
        reply_to: str | None = None,
    ) -> None: ...

    def send_photo(
        self,
        target: str,
        photo_bytes: bytes,
        *,
        caption: str = "",
    ) -> None: ...

    def send_document(
        self,
        target: str,
        file_path: str,
        *,
        caption: str = "",
    ) -> None: ...

    def send_reaction(
        self,
        target: str,
        message_id: str,
        emoji: str,
    ) -> None: ...

    def session_dir(self) -> Path:
        """Return the path where this backend stores auth/session state.

        Used by backup tooling so it knows what to snapshot.
        """
        ...
