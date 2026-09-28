#!/usr/bin/env python3
"""Fail when a Celery task is enqueued outside ``transaction.on_commit`` (standard §7.2/§8; CLAUDE.md Services).

A task enqueued inside a transaction can run before the commit (it reads stale rows) or after a rollback (it acts on
data that never existed). Every ``.delay(`` / ``.apply_async(`` (or a bare reference to either) outside ``*/tasks.py``
must therefore be either

* inside the callable handed to ``transaction.on_commit(...)`` — ``on_commit(lambda: task.delay(x))`` or
  ``on_commit(partial(task.delay, x))``; or
* inside a helper function (``_enqueue_…``) whose **every** reference in the repository sits inside an
  ``on_commit(...)`` callable — ``on_commit(partial(_enqueue_render, uid), robust=True)`` — and which is referenced
  at least once.

``*/tasks.py`` (tasks chaining tasks run in the worker), tests and migrations are not checked. Name matching is
repository-wide and conservative: a helper name reused elsewhere outside ``on_commit`` is reported too.

    python scripts/check_task_enqueues.py [ROOT]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ENQUEUE_ATTRS = frozenset({"delay", "apply_async"})
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "var", "__pycache__", "migrations", "htmlcov", "staticfiles", "tests"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


class Module:
    """A parsed source file with a child → parent map (the script is also loaded by file path in tests)."""

    def __init__(self, relative: str, tree: ast.Module):
        self.relative = relative
        self.tree = tree
        self.parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node

    def ancestors(self, node: ast.AST):
        while node in self.parents:
            node = self.parents[node]
            yield node


def _is_on_commit(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "on_commit") or (isinstance(func, ast.Name) and func.id == "on_commit")


def _in_on_commit_callable(module: Module, node: ast.AST) -> bool:
    """True when ``node`` lies inside the callable argument (first positional or ``func=``) of an ``on_commit`` call."""
    child = node
    for ancestor in module.ancestors(node):
        if _is_on_commit(ancestor):
            callable_arg = ancestor.args[0] if ancestor.args else next((kw.value for kw in ancestor.keywords if kw.arg == "func"), None)
            return child is callable_arg
        child = ancestor
    return False


def _enclosing_function(module: Module, node: ast.AST) -> ast.AST | None:
    return next((ancestor for ancestor in module.ancestors(node) if isinstance(ancestor, (*FUNCTIONS, ast.Lambda))), None)


def modules(root: Path):
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]) or relative.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - black/flake8 fail first
            raise SystemExit(f"{relative}: {exc}") from exc
        yield Module(relative.as_posix(), tree)


def _references(all_modules: list[Module], name: str):
    """Every load of ``name`` (``name`` or ``obj.name``) across the repository, except its own definitions."""
    for module in all_modules:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
                yield module, node
            elif isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.ctx, ast.Load):
                yield module, node


def violations(root: Path) -> list[str]:
    all_modules = list(modules(Path(root)))
    found: list[str] = []
    for module in all_modules:
        if module.relative.endswith("tasks.py"):
            continue
        for node in ast.walk(module.tree):
            if not (isinstance(node, ast.Attribute) and node.attr in ENQUEUE_ATTRS):
                continue
            where = f"{module.relative}:{node.lineno}"
            if _in_on_commit_callable(module, node):
                continue
            function = _enclosing_function(module, node)
            if function is None or isinstance(function, ast.Lambda):
                found.append(f"{where} .{node.attr}() outside transaction.on_commit")
                continue
            references = list(_references(all_modules, function.name))
            outside = [f"{ref_module.relative}:{ref.lineno}" for ref_module, ref in references if not _in_on_commit_callable(ref_module, ref)]
            if not references:
                found.append(f"{where} .{node.attr}() in {function.name}(), which is never scheduled with transaction.on_commit")
            elif outside:
                found.append(f"{where} .{node.attr}() in {function.name}(), which is also called outside transaction.on_commit ({', '.join(outside)})")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=Path(__file__).resolve().parent.parent, type=Path)
    args = parser.parse_args(argv)
    found = violations(args.root)
    for violation in found:
        print(f"{violation} — enqueue Celery tasks only from a transaction.on_commit callback", file=sys.stderr)
    if not found:
        print("task enqueues ok: every .delay()/.apply_async() runs from transaction.on_commit")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
