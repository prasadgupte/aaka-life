"""
skills/contacts/format.py — User-facing message renderers for birthday lists.

All Telegram/WhatsApp strings produced by the birthday-list intent live here.
Edit here to tweak entry layout, headers, hint lines.
"""

import urllib.parse
from datetime import date, timedelta


def wish_url(entry: dict, full: "dict | None" = None) -> str:
    """Build a WhatsApp wish URL for a contact. Empty string if no phone."""
    mobile = (full or {}).get("mobile") or entry.get("mobile", "")
    if not mobile or not mobile.strip("+"):
        return ""
    first = entry.get("first_name") or entry["name"].split()[0]
    msg = urllib.parse.quote(f"Happy Birthday {first}! 🎂")
    phone = mobile.lstrip("+")
    return f"https://wa.me/{phone}?text={msg}"


def format_date_header(d: date) -> str:
    today = date.today()
    if d == today:
        return f"🎂 Today — {d.strftime('%a %d %b')}"
    if d == today + timedelta(days=1):
        return f"🎂 Tomorrow — {d.strftime('%a %d %b')}"
    return f"🎂 {d.strftime('%a %d %b')}"


def format_entry(n: int, entry: dict, full: "dict | None" = None, age_year_resolver=None) -> str:
    """Format a single numbered birthday entry.

    Wraps the name as a [Name](wa.me/...) Markdown link when a phone is on file,
    falling back to plain text otherwise. Drops the trailing 📱 icon since the
    name itself is now tappable.

    age_year_resolver: optional callable(entry, full) -> int|None to compute age.
    """
    name = entry["name"]

    # Age: only from full cache (window strips birth year)
    age_str = ""
    if age_year_resolver:
        age = age_year_resolver(entry, full)
        if age is not None:
            age_str = f" ({age})"

    url = wish_url(entry, full)
    display = f"[{name}]({url})" if url else name
    return f"{n}. {display}{age_str}"


def render_today_birthdays(
    entries: "list[dict]",
    full_map: "dict[str, dict] | None" = None,
) -> str:
    """Compact "today's birthdays" block for embedding inside /today (D).

    Same Markdown-linked names as /bday — no 📱 icons. Falls back to plain
    name + email when phone is missing. Returns empty string if entries is
    empty.

    entries: list of dicts with {name, first_name, month_day, mobile, email}.
    full_map: optional name → full-record map (provides age + canonical phone).
    """
    if not entries:
        return ""
    full_map = full_map or {}
    today = date.today()
    lines = ["🎂 Today's birthdays:"]
    for entry in entries:
        name = entry["name"]
        full = full_map.get(name)

        age_str = ""
        if full and full.get("year"):
            age_str = f" ({today.year - full['year']})"

        url = wish_url(entry, full)
        display = f"[{name}]({url})" if url else name
        line = f"• {display}{age_str}"

        # If no phone, but we have email, surface it as a tap-to-mail link
        if not url:
            email = (full or {}).get("email") or entry.get("email", "")
            if email:
                line += f" — [✉️](mailto:{email})"
        lines.append(line)
    return "\n".join(lines)
