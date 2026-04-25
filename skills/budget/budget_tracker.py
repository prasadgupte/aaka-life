"""
skills/budget/budget_tracker.py — Sensor-side expense tracking.

Appends timestamped entries to per-month files in
$AAKA_CONFIG_DIR/data/budget/YYYY-MM.md. Synced to vault via file_sync.

Format per line: - YYYY-MM-DD AMOUNT CATEGORY [MERCHANT]
"""

import os
import re
from datetime import date, datetime
from pathlib import Path


def _budget_dir() -> Path:
    config_dir = Path(os.environ.get("AAKA_CONFIG_DIR", "/config"))
    d = config_dir / "data" / "budget"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


def _month_path(month: str = "") -> Path:
    if not month:
        month = _current_month()
    return _budget_dir() / f"{month}.md"


def _parse_entries(path: Path) -> list[dict]:
    """Parse budget entries from a month file.

    Returns list of {date, amount, category, merchant, line_num, raw}.
    """
    if not path.exists():
        return []
    entries = []
    for i, line in enumerate(path.read_text().split("\n")):
        m = re.match(r'^- (\d{4}-\d{2}-\d{2}) ([\d.]+) (\S+)(?: (.+))?$', line)
        if m:
            entries.append({
                "date": m.group(1),
                "amount": float(m.group(2)),
                "category": m.group(3),
                "merchant": (m.group(4) or "").strip(),
                "line_num": i,
                "raw": line,
            })
    return entries


def _resolve_month(arg: str) -> str:
    """Resolve a month name or YYYY-MM string to YYYY-MM."""
    arg = arg.strip().lower()
    if re.match(r'^\d{4}-\d{2}$', arg):
        return arg
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2,
        "mar": 3, "march": 3, "apr": 4, "april": 4,
        "may": 5, "jun": 6, "june": 6,
        "jul": 7, "july": 7, "aug": 8, "august": 8,
        "sep": 9, "september": 9, "oct": 10, "october": 10,
        "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    num = months.get(arg)
    if num:
        year = date.today().year
        return f"{year}-{num:02d}"
    return ""


def log_expense(amount: float, category: str, merchant: str = "",
                namespace: str = "user") -> str:
    """Append expense entry to current month file.

    Returns the relative path under data/ for file_sync.
    """
    month = _current_month()
    path = _month_path(month)
    is_new = not path.exists()
    today = date.today().isoformat()

    with open(path, "a") as f:
        if is_new:
            f.write(f"---\nowner: {namespace}\ntype: budget\n"
                    f"month: {month}\ncurrency: EUR\n---\n\n")
        line = f"- {today} {amount:.2f} {category}"
        if merchant:
            line += f" {merchant}"
        f.write(line + "\n")

    return f"data/budget/{path.name}"


def undo() -> str:
    """Remove the last entry from the current month file. Returns what was removed."""
    path = _month_path()
    entries = _parse_entries(path)
    if not entries:
        return "No entries to undo."
    last = entries[-1]
    lines = path.read_text().split("\n")
    lines.pop(last["line_num"])
    path.write_text("\n".join(lines))
    merch = f" {last['merchant']}" if last["merchant"] else ""
    return (f"Removed: {last['date']} {last['amount']:.2f} "
            f"{last['category']}{merch}")


def fix(index: int, amount: float) -> str:
    """Update amount of entry #index (1-based) in current month."""
    path = _month_path()
    entries = _parse_entries(path)
    if not entries:
        return "No entries to fix."
    if index < 1 or index > len(entries):
        return f"Entry #{index} not found. {len(entries)} entries this month."
    entry = entries[index - 1]
    lines = path.read_text().split("\n")
    merch = f" {entry['merchant']}" if entry["merchant"] else ""
    lines[entry["line_num"]] = (
        f"- {entry['date']} {amount:.2f} {entry['category']}{merch}"
    )
    path.write_text("\n".join(lines))
    return (f"Fixed #{index}: {entry['amount']:.2f} → {amount:.2f} "
            f"{entry['category']}{merch}")


def summary(month: str = "") -> str:
    """Category totals + bar chart + top merchants for a given month (zero-token)."""
    if not month:
        month = _current_month()
    path = _month_path(month)
    entries = _parse_entries(path)
    if not entries:
        # Pretty month label
        try:
            dt = datetime.strptime(month, "%Y-%m")
            label = dt.strftime("%B %Y")
        except ValueError:
            label = month
        return f"No expenses logged for {label}."

    total = sum(e["amount"] for e in entries)

    # Category totals
    cats: dict[str, float] = {}
    merchants: dict[str, int] = {}
    for e in entries:
        cats[e["category"]] = cats.get(e["category"], 0) + e["amount"]
        if e["merchant"]:
            merchants[e["merchant"]] = merchants.get(e["merchant"], 0) + 1

    # Sort by amount desc
    sorted_cats = sorted(cats.items(), key=lambda x: -x[1])

    # Month label
    try:
        dt = datetime.strptime(month, "%Y-%m")
        label = dt.strftime("%B %Y")
    except ValueError:
        label = month

    lines = [f"*{label}* — EUR {total:,.2f}\n"]
    max_bar = 8
    max_amt = sorted_cats[0][1] if sorted_cats else 1
    for cat, amt in sorted_cats:
        pct = (amt / total * 100) if total else 0
        bar_len = max(1, int(amt / max_amt * max_bar))
        bar = "\u2588" * bar_len
        lines.append(f"  {cat:<14s} {amt:>8.2f}  ({pct:2.0f}%)  {bar}")

    lines.append(f"\n  {len(entries)} entries \u00b7 avg {total / len(entries):.2f}/entry")

    if merchants:
        top = sorted(merchants.items(), key=lambda x: -x[1])[:3]
        merch_str = ", ".join(f"{m} ({c})" for m, c in top)
        lines.append(f"  top merchants: {merch_str}")

    return "\n".join(lines)


def yearly(year: int) -> str:
    """Month-by-month totals + category breakdown for a year (zero-token)."""
    budget_dir = _budget_dir()
    monthly_totals: list[tuple[str, float]] = []
    all_cats: dict[str, float] = {}
    grand_total = 0.0

    for m in range(1, 13):
        month_str = f"{year}-{m:02d}"
        path = budget_dir / f"{month_str}.md"
        entries = _parse_entries(path)
        month_total = sum(e["amount"] for e in entries)
        monthly_totals.append((month_str, month_total))
        grand_total += month_total
        for e in entries:
            all_cats[e["category"]] = all_cats.get(e["category"], 0) + e["amount"]

    if grand_total == 0:
        return f"No expenses logged for {year}."

    lines = [f"*{year}* — EUR {grand_total:,.2f}\n"]
    max_bar = 6
    active = [(ms, t) for ms, t in monthly_totals if t > 0]
    max_month = max(t for _, t in active) if active else 1

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for i, (_, total) in enumerate(monthly_totals):
        if total > 0:
            bar_len = max(1, int(total / max_month * max_bar))
            bar = "\u2588" * bar_len
            lines.append(f"  {month_names[i]}  {total:>8.2f}  {bar}")
        else:
            lines.append(f"  {month_names[i]}       —")

    # Top categories
    if all_cats:
        sorted_cats = sorted(all_cats.items(), key=lambda x: -x[1])[:3]
        cat_str = ", ".join(
            f"{c} ({v / grand_total * 100:.0f}%)" for c, v in sorted_cats
        )
        lines.append(f"\n  top categories: {cat_str}")

    if active:
        avg = grand_total / len(active)
        highest = max(active, key=lambda x: x[1])
        lowest = min(active, key=lambda x: x[1])
        h_name = month_names[int(highest[0].split("-")[1]) - 1]
        l_name = month_names[int(lowest[0].split("-")[1]) - 1]
        lines.append(f"  avg monthly: {avg:,.2f}")
        lines.append(
            f"  highest: {h_name} ({highest[1]:,.2f}) \u00b7 "
            f"lowest: {l_name} ({lowest[1]:,.2f})"
        )

    return "\n".join(lines)


def sync_dest(month: str = "", namespace: str = "user") -> str:
    """Return the vault-relative destination path for file_sync."""
    if not month:
        month = _current_month()
    return f"Finance/{namespace}/budget/{month}.md"
