#!/usr/bin/env python3
"""
admin/gmail_label_mapper.py — Auto-match Gmail labels to aaka tags.

Fetches every user-created Gmail label, fuzzy-matches each against tags/entities/
aliases in references.yaml, and lets you confirm which mappings to write.

Confirmed mappings are saved as  gmail_label: "Label/Name"  on the matching
route in references.yaml.  Once set, gmail_poller.py will auto-file all
attachments from emails tagged with that label to the route's vault folder.

Usage:
    python3 admin/gmail_label_mapper.py
    python3 admin/gmail_label_mapper.py --member alex   # use member's gmail
    python3 admin/gmail_label_mapper.py --dry-run         # show mapping, don't write
    python3 admin/gmail_label_mapper.py --min-score 0.5   # lower threshold (default 0.6)
"""
import argparse
import difflib
import os
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import aaka_config

REFS_FILE = aaka_config.CONFIG_DIR / "config" / "references.yaml"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normalise a string for fuzzy comparison: lowercase, strip punctuation/separators."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _score(a: str, b: str) -> float:
    """SequenceMatcher similarity between normalised forms."""
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _best_component_score(label_name: str, tag: str) -> float:
    """Score a Gmail label against a tag considering label path components.

    Gmail labels are often hierarchical: "AAKA/FLO172/Tax", "P301/Invoices".
    We match each component individually and take the best score, so
    "AAKA/FLO172" correctly hits tag "flo172" even though full-string score is lower.
    """
    parts = label_name.replace("\\", "/").split("/")
    scores = [_score(p, tag) for p in parts]
    scores.append(_score(label_name, tag))   # also try full path
    return max(scores)


def _load_refs() -> dict:
    import yaml
    if not REFS_FILE.exists():
        return {}
    return yaml.safe_load(REFS_FILE.read_text()) or {}


def _all_tags(refs: dict) -> "list[tuple[str, str, str]]":
    """Return all known tags as (tag_name, kind, hint).

    kind:  'route' | 'entity' | 'alias'
    hint:  where it routes to (path or entity key)
    """
    out: list[tuple[str, str, str]] = []

    for key in (refs.get("routes") or {}):
        route = refs["routes"][key]
        if isinstance(route, dict):
            path = route.get("path", key)
            owner = route.get("owner", "")
            hint = f"{owner}/{path}" if owner else path
            already = route.get("gmail_label", "")
        else:
            hint = str(route)
            already = ""
        out.append((key, "route", hint, already))  # type: ignore[arg-type]

    for key, ent in (refs.get("entities") or {}).items():
        path = ent.get("vault_path", key)
        out.append((key, "entity", path, ""))  # type: ignore[arg-type]
        for alias in (ent.get("aliases") or []):
            out.append((alias.lower(), "entity-alias", f"→ {key}", ""))  # type: ignore[arg-type]

    for key, target in (refs.get("aliases") or {}).items():
        out.append((key, "alias", f"→ {target}", ""))  # type: ignore[arg-type]

    return out  # type: ignore[return-value]


def _fetch_labels(member_id: str) -> "list[dict]":
    """Return all user-created Gmail labels [{id, name}], excluding system labels."""
    from skills.mail.gmail import get_gmail_service
    svc = get_gmail_service(member_id)
    result = svc.users().labels().list(userId="me").execute()
    labels = result.get("labels", [])
    # Filter: skip system labels (INBOX, SENT, SPAM, TRASH, UNREAD, CATEGORY_*, etc.)
    user_labels = [
        lbl for lbl in labels
        if lbl.get("type") == "user"
    ]
    return sorted(user_labels, key=lambda l: l["name"])


def _match(labels: "list[dict]", tags: list, min_score: float) -> "list[dict]":
    """For each Gmail label find the best-matching tag above min_score."""
    matches = []
    for lbl in labels:
        name = lbl["name"]
        best_score = 0.0
        best_tag   = None
        best_kind  = None
        best_hint  = None
        best_existing = None

        for row in tags:
            tag_name, kind, hint, already = row
            s = _best_component_score(name, tag_name)
            if s > best_score:
                best_score    = s
                best_tag      = tag_name
                best_kind     = kind
                best_hint     = hint
                best_existing = already

        matches.append({
            "label_name": name,
            "label_id":   lbl["id"],
            "tag":        best_tag,
            "kind":       best_kind,
            "hint":       best_hint,
            "score":      best_score,
            "existing":   best_existing,   # current gmail_label field value if any
            "above_threshold": best_score >= min_score,
        })
    return matches


def _write_gmail_label(refs: dict, tag_name: str, label_name: str) -> bool:
    """Set gmail_label on the route for tag_name. Converts string routes to dicts.

    Returns True if the file was modified.
    """
    import yaml

    routes = refs.setdefault("routes", {})
    if tag_name not in routes:
        # Create a minimal route entry (user can fill path later)
        routes[tag_name] = {"path": tag_name, "gmail_label": label_name}
    else:
        route = routes[tag_name]
        if isinstance(route, str):
            # Upgrade string route to dict
            routes[tag_name] = {"path": route, "gmail_label": label_name}
        else:
            route["gmail_label"] = label_name

    REFS_FILE.write_text(yaml.dump(refs, allow_unicode=True, sort_keys=False, default_flow_style=False))
    return True


# ── Display ────────────────────────────────────────────────────────────────────

_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_DIM    = "\033[2m"
_BOLD   = "\033[1m"
_RED    = "\033[31m"
_NC     = "\033[0m"


def _fmt_score(s: float) -> str:
    if s >= 0.85:
        return f"{_GREEN}{s:.0%}{_NC}"
    if s >= 0.65:
        return f"{_YELLOW}{s:.0%}{_NC}"
    return f"{_RED}{s:.0%}{_NC}"


def _print_table(matches: list, show_all: bool = False) -> None:
    print(f"\n{_BOLD}Gmail label → aaka tag matches{_NC}")
    print("─" * 80)
    fmt = f"  {{:<40}} {{:<18}} {{:<8}} {{}}"
    print(fmt.format("Gmail label", "Tag", "Score", "Route / hint"))
    print("─" * 80)
    for m in matches:
        if not show_all and not m["above_threshold"]:
            continue
        tag   = m["tag"] or "—"
        score = _fmt_score(m["score"]) if m["tag"] else _DIM + "—" + _NC
        hint  = m["hint"] or ""
        kind_note = f"{_DIM}({m['kind']}){_NC}" if m["kind"] != "route" else ""
        extra = f"{hint} {kind_note}".strip()
        if m["existing"]:
            extra += f"  {_CYAN}[already: {m['existing']}]{_NC}"
        print(fmt.format(m["label_name"][:40], tag[:18], score, extra))
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--member", default="", help="Member ID for Gmail auth (default: first auth member)")
    parser.add_argument("--dry-run", action="store_true", help="Show matches, don't write")
    parser.add_argument("--min-score", type=float, default=0.6,
                        help="Minimum match score to show as a candidate (default 0.6)")
    parser.add_argument("--all", action="store_true",
                        help="Show all labels including low-confidence matches")
    args = parser.parse_args()

    print(f"{_BOLD}Fetching Gmail labels...{_NC}", flush=True)
    try:
        labels = _fetch_labels(args.member)
    except Exception as exc:
        print(f"{_RED}Gmail auth failed: {exc}{_NC}", file=sys.stderr)
        sys.exit(1)
    print(f"  Found {len(labels)} user label(s)")

    refs = _load_refs()
    if not refs:
        print(f"{_RED}references.yaml not found at {REFS_FILE}{_NC}", file=sys.stderr)
        sys.exit(1)

    tags = _all_tags(refs)
    matches = _match(labels, tags, args.min_score)
    above = [m for m in matches if m["above_threshold"]]
    below = [m for m in matches if not m["above_threshold"]]

    _print_table(matches, show_all=args.all)

    if not above:
        print(f"{_DIM}No matches above {args.min_score:.0%} threshold. "
              f"Try --min-score 0.4 or --all to see everything.{_NC}")
        return

    print(f"Found {len(above)} candidate mapping(s). "
          f"{len(below)} label(s) below threshold (use --all to see).\n")

    if args.dry_run:
        print(f"{_DIM}Dry run — nothing written.{_NC}")
        return

    # ── Confirm each candidate ─────────────────────────────────────────────────
    confirmed: list[dict] = []
    skipped:   list[dict] = []

    print(f"{_BOLD}Confirm each mapping (y/n/q to quit):{_NC}\n")

    for m in above:
        if m["existing"] and m["existing"] == m["label_name"]:
            print(f"  {_DIM}SKIP  {m['label_name']!r} → already mapped to {m['tag']!r}{_NC}")
            skipped.append(m)
            continue

        kind_warn = ""
        if m["kind"] != "route":
            kind_warn = (f"\n       {_YELLOW}⚠ '{m['tag']}' is a {m['kind']}, not a route. "
                         f"A route entry will be created.{_NC}")

        prompt = (f"  Map {_BOLD}{m['label_name']!r}{_NC}"
                  f"  →  {_CYAN}{m['tag']!r}{_NC}"
                  f"  ({m['hint']}, {_fmt_score(m['score'])})"
                  f"{kind_warn}"
                  f"\n  [y/n/q] ")

        while True:
            try:
                ans = input(prompt).strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\nAborted.")
                sys.exit(0)
            if ans in ("y", "yes"):
                confirmed.append(m)
                break
            elif ans in ("n", "no", ""):
                skipped.append(m)
                break
            elif ans in ("q", "quit"):
                print("Quit.")
                sys.exit(0)

    print()

    if not confirmed:
        print("No mappings confirmed — nothing written.")
        return

    # ── Write confirmed mappings ───────────────────────────────────────────────
    # Reload refs fresh before writing (in case something changed)
    import yaml
    refs = yaml.safe_load(REFS_FILE.read_text()) or {}

    written = []
    for m in confirmed:
        tag_name   = m["tag"]
        label_name = m["label_name"]

        # If it's not a route, we need to create one
        if m["kind"] != "route":
            # For entity-alias: route to same path as entity
            # For entity: use entity vault_path
            hint_path = m["hint"].lstrip("→ ").strip()
            routes = refs.setdefault("routes", {})
            if tag_name not in routes:
                routes[tag_name] = {"path": hint_path, "gmail_label": label_name}
                written.append((tag_name, label_name, "created route"))
            else:
                _write_gmail_label(refs, tag_name, label_name)
                written.append((tag_name, label_name, "updated route"))
        else:
            _write_gmail_label(refs, tag_name, label_name)
            written.append((tag_name, label_name, "updated route"))

    # Single write at end
    REFS_FILE.write_text(yaml.dump(refs, allow_unicode=True, sort_keys=False, default_flow_style=False))

    print(f"{_GREEN}Written {len(written)} mapping(s) to {REFS_FILE}:{_NC}")
    for tag_name, label_name, action in written:
        print(f"  {_CYAN}{tag_name}{_NC} ← {label_name!r}  ({action})")

    print(f"\n{_DIM}The gmail_poller (cron every 10 min) will now auto-file attachments "
          f"from emails with these labels.{_NC}")
    print(f"{_DIM}To immediately process any existing emails: "
          f"python3 sensor/gmail_poller.py --reprocess \"<label>\"{_NC}\n")


if __name__ == "__main__":
    main()
