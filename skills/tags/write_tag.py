"""
Executor skill: write_tag

Writes alias/route/description/retire changes to references.yaml on Mac.
Enqueued by the sensor's tag_manage handler so writes happen on the
executor (single writer) and rsync propagates the result to VPS.

Payload keys (all optional, use whichever action applies):
  action          : "alias" | "route" | "desc" | "retire"
  tag_name        : str
  alias_target    : str   (for action=alias)
  route_path      : str   (for action=route)
  route_owner     : str   (for action=route, optional)
  description     : str   (for action=desc)
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from tools.inbox_router import write_tag_to_references, retire_tag


def execute(payload: dict) -> dict:
    action = payload.get("action", "")
    tag_name = payload.get("tag_name", "")

    if action == "alias":
        written = write_tag_to_references(alias=tag_name, alias_target=payload["alias_target"])
        return {"status": "ok", "action": "alias", "tag": tag_name, "written": written}

    elif action == "route":
        written = write_tag_to_references(
            route_key=tag_name,
            route_path=payload["route_path"],
            route_owner=payload.get("route_owner"),
        )
        return {"status": "ok", "action": "route", "tag": tag_name, "written": written}

    elif action == "desc":
        written = write_tag_to_references(
            description_key=tag_name,
            description_value=payload["description"],
        )
        return {"status": "ok", "action": "desc", "tag": tag_name, "written": written}

    elif action == "retire":
        result = retire_tag(tag_name)
        return {"status": "ok", "action": "retire", "tag": tag_name, **result}

    else:
        raise ValueError(f"write_tag: unknown action {action!r}")
