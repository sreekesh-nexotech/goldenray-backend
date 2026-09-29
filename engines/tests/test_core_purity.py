"""engines-core is pure Python: standard library and ``engines`` only (import-linter enforces the Django side too)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

CORE = ("money", "energy", "savings", "subsidy", "finance")
ENGINES = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("module", CORE)
def test_imports_only_the_standard_library_and_engines(module):
    foreign = {name for name in _imports(ENGINES / f"{module}.py") if name != "engines" and name not in sys.stdlib_module_names and name != "__future__"}
    assert not foreign, foreign


@pytest.mark.parametrize("module", CORE)
def test_never_reads_the_clock_or_the_environment(module):
    source = (ENGINES / f"{module}.py").read_text(encoding="utf-8")
    for forbidden in ("datetime.now", "time.time", "date.today", "os.environ", "random"):
        assert forbidden not in source, forbidden


def test_floats_appear_only_in_the_bill_search_replica():
    """``float(...)`` and float literals live only in the binary64 replica of the JavaScript bill→units search."""
    allowed = {
        "energy": {"_Binary64Region", "_binary64_math_round", "_binary64_first", "_binary64_energy", "_binary64_bill_total", "bill_to_units"},
        # the boundary: floats are recognised in order to be refused, or converted from parsed JSON by their repr
        "money": {"json_number"},
    }
    for module in CORE:
        tree = ast.parse((ENGINES / f"{module}.py").read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            uses_float = (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float") or (isinstance(node, ast.Constant) and isinstance(node.value, float))
            if not uses_float:
                continue
            scopes, parent = [], parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.ClassDef)):
                    scopes.append(parent.name)
                parent = parents.get(parent)
            assert set(scopes) & allowed.get(module, set()), f"float in {module}.{'.'.join(reversed(scopes)) or '<module>'} (line {node.lineno})"
