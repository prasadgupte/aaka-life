"""sensor/intents/lists.py — Buy/shopping list handlers."""
import re

import aaka_config
from aaka_queue.queue import set_pending_confirm
from sensor.helpers import queue_file_sync

HANDLES = frozenset({"buy_list"})


def handle(intent: str, message: str, sender: str, channel_id: str, source: str) -> str:
    if intent == "buy_list":
        from skills.lists.list_manager import (
            add_items, show, show_all, audit, check_off, clear_done, done_all,
            list_all, share_list, unshare_list, is_shared,
            preview_done_all, preview_clear,
        )
        from sensor.router_sensor import _namespace_for_sender, _strip_me_flag
        namespace = _namespace_for_sender(sender) or "shared"
        member_obj = aaka_config.member_by_sender(sender)
        member_id = member_obj["id"] if member_obj else None
        can_shared = aaka_config.member_can_access_shared(member_id) if member_id else False

        text = re.sub(r'^/buy\s*', '', message, flags=re.I).strip()
        text, me_flag = _strip_me_flag(text)
        if not text:
            return list_all(namespace, include_shared=(False if me_flag else can_shared))

        _BUY_HELP = (
            "📋 *Buy Lists — commands*\n\n"
            "*View*\n"
            "b  ·  all lists\n"
            "b <list>  ·  open list (numbered for check-off)\n"
            "b <list> ?  ·  ideas (things we usually buy here)\n"
            "b <list> #all  ·  include done items\n"
            "b <list> audit  ·  show who added each item & when\n\n"
            "*Add items*\n"
            "b <list> item1, item2  ·  add one or more items\n"
            "b <list> add 1 3 5  ·  add items by number from ideas view\n\n"
            "*Check off*\n"
            "b <list>  then reply  1 3  ·  check off by number\n"
            "b <list> #done  ·  mark all done (asks confirm)\n\n"
            "*Organise*\n"
            "b <list> #share  ·  share list with family 👥\n"
            "b <list> #private  ·  move back to personal\n"
            "b <list> #clear  ·  archive done items (asks confirm)"
        )

        if text.lower() in ("help", "#help"):
            return _BUY_HELP

        if text.lower() in ("#done", "#clear", "#all", "#share", "#private"):
            return f"{list_all(namespace, include_shared=can_shared)}\n↪ b <list> {text}"

        lines_input = [l.strip() for l in text.split("\n") if l.strip()]
        first_words = lines_input[0].split()
        list_name = first_words[0].lower()
        rest = " ".join(first_words[1:]).strip()

        if not can_shared and is_shared(list_name):
            return "🔒 Shared lists are not available to you."

        if rest.lower() in ("help", "#help"):
            return _BUY_HELP

        if rest.lower() in ("#share", "share"):
            result = share_list(list_name, namespace)
            queue_file_sync(list_name, sender, channel_id, source="sensor")
            return result

        if rest.lower() in ("#private", "private"):
            result = unshare_list(list_name, namespace)
            queue_file_sync(list_name, sender, channel_id, source="sensor")
            return result

        if rest.lower() in ("#all", "all"):
            return show_all(list_name, namespace)

        if rest.lower() == "audit":
            return audit(list_name, namespace)

        if rest.lower() in ("?", "ideas", "#ideas"):
            from skills.lists.list_manager import ideas
            from skills.lists.format import render_ideas_view
            data = ideas(list_name, namespace)
            set_pending_confirm(sender, f"list-ideas:{list_name}")
            return render_ideas_view(data)

        # "b <list> add 1 3 5" — add by number from ideas pool, no pending state needed.
        if first_words[1].lower() == "add" if len(first_words) > 1 else False:
            from skills.lists.list_manager import ideas, add_items, list_state
            from skills.lists.format import render_list_view
            nums_part = " ".join(first_words[2:]).strip()
            nums = re.findall(r'\b(\d+)\b', nums_part)
            if not nums:
                return f"↪ b {list_name} add 1 3 5  ·  pick items from `b {list_name} ?`"
            data = ideas(list_name, namespace)
            picked = []
            for n in nums:
                idx = int(n) - 1
                if 0 <= idx < len(data["ideas"]):
                    picked.append(data["ideas"][idx]["item"])
            if not picked:
                return f"No ideas at those numbers in *{list_name}*."
            add_items(list_name, picked, namespace)
            queue_file_sync(list_name, sender, channel_id, source="sensor")
            state = list_state(list_name, namespace)
            set_pending_confirm(sender, f"list:{list_name}")
            return render_list_view(
                state["list_name"], state["unchecked"], state["checked_count"], state["shared"],
                added_count=len(picked),
            )

        if rest.lower() in ("#done", "done") and len(first_words) == 2:
            preview = preview_done_all(list_name, namespace)
            set_pending_confirm(sender, f"list-done:{list_name}")
            return preview

        if rest.lower() in ("#clear", "clear"):
            preview = preview_clear(list_name, namespace)
            set_pending_confirm(sender, f"list-clear:{list_name}")
            return preview

        if not rest and len(lines_input) == 1:
            set_pending_confirm(sender, f"list:{list_name}")
            return show(list_name, namespace)

        if first_words[1].lower() == "done" if len(first_words) > 1 else False:
            hint = " ".join(first_words[2:]).strip()
            if not hint:
                return show(list_name, namespace)
            result = check_off(list_name, hint, namespace)
            queue_file_sync(list_name, sender, channel_id, source="sensor")
            return result

        items = []
        if rest:
            items.append(rest)
        items.extend(lines_input[1:])
        added = add_items(list_name, items, namespace)
        queue_file_sync(list_name, sender, channel_id, source="sensor")
        from skills.lists.list_manager import list_state
        from skills.lists.format import render_list_view
        state = list_state(list_name, namespace)
        set_pending_confirm(sender, f"list:{list_name}")
        if state is None:
            from skills.lists.format import render_add_confirm
            return render_add_confirm(list_name, added["added"], added["total"])
        return render_list_view(
            state["list_name"], state["unchecked"], state["checked_count"], state["shared"],
            added_count=len(added["added"]),
        )

    return "❓ Unknown lists intent."
