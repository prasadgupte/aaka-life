"""sensor/intents/misc.py — Miscellaneous local handlers (pdf_tool)."""

HANDLES = frozenset({"pdf_tool"})


def handle(intent: str, message: str, sender: str, channel_id: str, source: str) -> str:
    if intent == "pdf_tool":
        try:
            from tools.pdf_tool import help_text as _pdf_help
            return _pdf_help()
        except ImportError:
            return "PDF tool not available (pymupdf not installed)."

    return "❓ Unknown misc intent."
