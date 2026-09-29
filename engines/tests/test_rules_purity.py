"""engines-rules is pure Python (standard library and ``engines`` only), deterministic, and float-free; the committed
golden files are exactly what the generator produces from the real JavaScript."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MODULES = ("jscompat", "frozen", "bom_domain", "engineering_checker", "gate", "quotation_payload", "content_fit")
ENGINES = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden"
FLARIZE_ROOT = Path(os.environ.get("FLARIZE_ROOT", "/home/user/flarize-main/flarize"))
NODE = shutil.which("node") or ("/opt/node22/bin/node" if Path("/opt/node22/bin/node").exists() else None)


def _tree(module: str) -> ast.Module:
    return ast.parse((ENGINES / f"{module}.py").read_text(encoding="utf-8"))


@pytest.mark.parametrize("module", MODULES)
def test_imports_only_the_standard_library_and_engines(module):
    names = set()
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    foreign = {name for name in names if name not in ("engines", "__future__") and name not in sys.stdlib_module_names}
    assert not foreign, foreign


@pytest.mark.parametrize("module", MODULES)
def test_never_reads_the_clock_the_environment_or_randomness(module):
    source = (ENGINES / f"{module}.py").read_text(encoding="utf-8")
    for forbidden in ("datetime.now", "date.today", "time.time", "os.environ", "import random", "uuid"):
        assert forbidden not in source, forbidden


def test_floats_appear_only_at_the_json_boundary():
    """No float arithmetic: ``float(...)`` / float literals only where a JSON float is recognised or a parameter is
    written back for JSONB storage."""
    allowed = {"engineering_checker": {"_plain"}, "frozen": {"jsonable"}}
    for module in MODULES:
        tree = _tree(module)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            used = (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float") or (isinstance(node, ast.Constant) and isinstance(node.value, float))
            if not used:
                continue
            scopes, parent = [], parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.ClassDef)):
                    scopes.append(parent.name)
                parent = parents.get(parent)
            assert set(scopes) & allowed.get(module, set()), f"float in {module}.{'.'.join(reversed(scopes)) or '<module>'} (line {node.lineno})"


def test_golden_headers_pin_the_javascript_sources():
    for path in sorted(GOLDEN.glob("rules_*.json")):
        header = json.loads(path.read_text(encoding="utf-8"))["header"]
        assert header["schema"] == "engines-rules.golden/1"
        assert "src/lib/engineeringChecker.js" in header["sources"] and len(header["sources"]["src/lib/engineeringChecker.js"]) == 64


@pytest.mark.slow
@pytest.mark.skipif(NODE is None or not (FLARIZE_ROOT / "src" / "lib" / "engineeringChecker.js").exists(), reason="node or the Flarize sources are not available")
def test_golden_capture_is_reproducible(tmp_path):
    """Re-running the generator against the real JavaScript yields byte-identical golden files."""
    environment = {**os.environ, "GOLDEN_OUT": str(tmp_path), "FLARIZE_ROOT": str(FLARIZE_ROOT), "TZ": "UTC"}
    subprocess.run([NODE, "--no-warnings", str(GOLDEN / "generate_rules.mjs")], check=True, env=environment, capture_output=True, timeout=600)
    produced = sorted(path.name for path in tmp_path.glob("rules_*.json"))
    assert produced == sorted(path.name for path in GOLDEN.glob("rules_*.json"))
    for name in produced:
        assert (tmp_path / name).read_bytes() == (GOLDEN / name).read_bytes(), name
