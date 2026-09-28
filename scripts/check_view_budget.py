#!/usr/bin/env python3
"""Fail when any file under an app's ``views/`` package exceeds the view budget (CLAUDE.md: ≤ 250 lines per file).

Views stay thin (HTTP shape only); a long view file means business logic leaked out of ``services/``.

    python scripts/check_view_budget.py [--max-lines 250] [ROOT]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_MAX_LINES = 250
SKIP_DIRS = {".claude", ".git", ".venv", "venv", "node_modules", "var", "__pycache__", "migrations", "htmlcov", "staticfiles"}


def view_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if "views" in relative.parts[:-1]:
            yield path


def count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def over_budget(root: Path, max_lines: int = DEFAULT_MAX_LINES) -> list[tuple[str, int]]:
    offenders = []
    for path in view_files(root):
        lines = count_lines(path)
        if lines > max_lines:
            offenders.append((str(path.relative_to(root)), lines))
    return offenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=Path(__file__).resolve().parent.parent, type=Path)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    args = parser.parse_args(argv)
    offenders = over_budget(args.root, args.max_lines)
    for name, lines in offenders:
        print(f"{name}: {lines} lines (budget {args.max_lines}) — move logic into services/ or split the view module", file=sys.stderr)
    if not offenders:
        print(f"view budget ok: every views/ file is ≤ {args.max_lines} lines")
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main())
