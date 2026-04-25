#!/usr/bin/env python3
"""
tools/dependency_graph.py — Print skill dependency graph from skills/registry.yaml.

Usage:
    python3 tools/dependency_graph.py
    python3 tools/dependency_graph.py --dot   # Graphviz DOT format

Each skill shows its intent, enabled status, and depends_on links.
"""

import argparse
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("AAKA_BASE") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(BASE))


def load_registry() -> dict:
    import yaml
    p = BASE / "skills" / "registry.yaml"
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("skills", {})


def print_text(registry: dict) -> None:
    print("Skill Dependency Graph\n" + "=" * 40)
    for key, cfg in sorted(registry.items()):
        enabled = "✓" if cfg.get("enabled", True) else "✗"
        intent = cfg.get("intent", "—")
        deps = cfg.get("depends_on") or []
        dep_str = (" → [" + ", ".join(deps) + "]") if deps else ""
        print(f"  [{enabled}] {key} (intent={intent}){dep_str}")

    print()
    print("Disabled skills:")
    for key, cfg in sorted(registry.items()):
        if not cfg.get("enabled", True):
            dependents = [
                k for k, c in registry.items()
                if key in (c.get("depends_on") or [])
            ]
            dep_str = f"  ← blocked by: {', '.join(dependents)}" if dependents else ""
            print(f"  ✗ {key}{dep_str}")


def print_dot(registry: dict) -> None:
    print("digraph skills {")
    print('  rankdir=LR;')
    print('  node [shape=box, style=filled];')
    for key, cfg in registry.items():
        enabled = cfg.get("enabled", True)
        color = "lightgreen" if enabled else "lightcoral"
        print(f'  "{key}" [fillcolor="{color}"];')
    for key, cfg in registry.items():
        for dep in (cfg.get("depends_on") or []):
            print(f'  "{key}" -> "{dep}";')
    print("}")


def main():
    parser = argparse.ArgumentParser(description="Print skill dependency graph")
    parser.add_argument("--dot", action="store_true", help="Output Graphviz DOT format")
    args = parser.parse_args()
    registry = load_registry()
    if args.dot:
        print_dot(registry)
    else:
        print_text(registry)


if __name__ == "__main__":
    main()
