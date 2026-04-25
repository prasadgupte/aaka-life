"""
LinkedIn post skill — delegates to /Users/Shared/linkedin-tool/linkedin.py.
Called by executor when a linkedin_post queue item is confirmed + ready.
"""

import sys
from pathlib import Path

LINKEDIN_TOOL = Path("/Users/Shared/tools/linkedin-tool")


def post(text: str, image_path: str | None = None, visibility: str = "PUBLIC", schedule_at: str | None = None) -> dict:
    """Create a LinkedIn post. Returns {post_id, text, image}.

    schedule_at: ISO 8601 UTC string (e.g. "2026-05-18T09:00:00Z") to schedule
                 the post instead of publishing immediately.
    """
    sys.path.insert(0, str(LINKEDIN_TOOL))
    try:
        import linkedin as li
        if image_path:
            result = li.create_image_post(text, image_path, visibility=visibility, schedule_at=schedule_at)
        else:
            result = li.create_text_post(text, visibility=visibility, schedule_at=schedule_at)
        post_id = result.get("id", result.get("X-RestLi-Id", "unknown"))
        return {"post_id": post_id, "text": text, "image": image_path, "scheduled": bool(schedule_at)}
    finally:
        sys.path.remove(str(LINKEDIN_TOOL))
