"""
gateway/backends/openclaw.py — WhatsApp via the OpenClaw CLI.

Shells out to the `openclaw` binary (installed in the sensor Docker image).
This is the only file that knows openclaw's subprocess interface for outbound
WhatsApp — all other code should call this module, not shell out directly.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _claw_bin() -> str:
    return os.environ.get("CLAW_BIN", "openclaw")


class OpenClawBackend:
    """WhatsApp outbound via the openclaw CLI subprocess."""

    def send_text(
        self,
        target: str,
        text: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        cmd = [
            _claw_bin(), "message", "send",
            "--channel", "whatsapp",
            "--target", target,
            "--message", text,
        ]
        if reply_to:
            cmd += ["--reply-to", str(reply_to)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            if reply_to:
                # Retry without --reply-to in case the backend doesn't support it
                cmd_no_reply = [a for a in cmd if a not in ("--reply-to", str(reply_to))]
                result = subprocess.run(cmd_no_reply, capture_output=True, text=True)
                if result.returncode == 0:
                    return
            raise RuntimeError(
                f"[openclaw] send_text failed: {(result.stderr or result.stdout).strip()}"
            )

    def send_photo(
        self,
        target: str,
        photo_bytes: bytes,
        *,
        caption: str = "",
    ) -> None:
        raise NotImplementedError("OpenClaw WA photo send not implemented")

    def send_document(
        self,
        target: str,
        file_path: str,
        *,
        caption: str = "",
    ) -> None:
        raise NotImplementedError("OpenClaw WA document send not implemented")

    def send_reaction(
        self,
        target: str,
        message_id: str,
        emoji: str,
    ) -> None:
        pass  # WA reactions are not exposed via the OpenClaw CLI

    def session_dir(self) -> Path:
        # Auth state lives in the openclaw-data volume mounted into the container.
        # config_dir is /opt/aaka-config on VPS; openclaw-data is a sibling dir.
        config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
        candidate = config_dir.parent / "openclaw-data"
        if candidate.exists():
            return candidate
        # Fallback: ~/.openclaw inside the container
        return Path("/home/aaka/.openclaw")
