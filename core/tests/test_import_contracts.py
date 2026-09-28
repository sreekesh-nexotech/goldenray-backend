"""The import-linter contracts (``.importlinter``) really forbid what CLAUDE.md and PLAN §1.1 say they forbid.

``lint-imports`` in CI only proves that today's code keeps the contracts. These tests prove the contracts
themselves: they run import-linter with the repository's configuration against stub packages that contain one probe
import per rule, so a contract that silently stops covering an app (or a new app nobody added) fails here.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
CONFIG = configparser.ConfigParser()
CONFIG.read(ROOT / ".importlinter")

CUTOVER_ONLY = {"legacy", "migrations_tools"}  # deleted after the legacy cutover (PLAN §6.4)
WEBSITE_CONTENT = {"blog", "sitepages", "faqs", "careers", "seo", "company", "reference"}
HR = {"hr", "attendance", "devices"}
PRODUCT_MASTER_AND_CONFIGURATION = {"catalog", "pricing", "procurement", "inventory", "bom", "packs", "engineering"}


def _modules(contract: str, key: str) -> set[str]:
    return set(CONFIG[f"importlinter:contract:{contract}"][key].split())


def _lint_imports() -> str:
    candidate = Path(sys.executable).with_name("lint-imports")
    return str(candidate) if candidate.exists() else (shutil.which("lint-imports") or "lint-imports")


def test_every_app_is_a_root_package():
    assert set(CONFIG["importlinter"]["root_packages"].split()) == set(settings.LOCAL_APPS) | {"flarize", "engines"}


def test_product_master_and_configuration_never_import_website_content_or_hr():
    contract = "product-master-ignores-content-and-hr"
    assert _modules(contract, "source_modules") == PRODUCT_MASTER_AND_CONFIGURATION
    assert WEBSITE_CONTENT | HR <= _modules(contract, "forbidden_modules")


def test_no_package_imports_the_cutover_only_packages():
    contract = "nothing-imports-cutover-packages"
    roots = set(CONFIG["importlinter"]["root_packages"].split())
    assert _modules(contract, "source_modules") == roots - CUTOVER_ONLY
    assert _modules(contract, "forbidden_modules") == CUTOVER_ONLY


BROKEN = [
    ("catalog", "company"),
    ("pricing", "company.services"),
    ("bom", "company"),
    ("engineering", "company"),
    ("catalog", "legacy"),
    ("quotations", "migrations_tools"),
    ("flarize", "legacy.urls"),
    ("hr", "migrations_tools"),
    ("packs", "calculators"),  # control: a rule that was already enforced
]
ALLOWED = [
    ("legacy", "catalog"),  # the adapters read the new tables
    ("migrations_tools", "legacy"),
    ("quotations", "company"),  # sales reads the company profile (quotation offer, letterhead)
]


@pytest.mark.slow
def test_probe_imports_are_reported(tmp_path):
    roots = CONFIG["importlinter"]["root_packages"].split()
    for root in roots:
        (tmp_path / root).mkdir()
        (tmp_path / root / "__init__.py").write_text("")
    for source, target in BROKEN + ALLOWED:
        (tmp_path / source / f"_probe_{target.replace('.', '_')}.py").write_text(f"import {target}\n")
    (tmp_path / "company" / "services").mkdir()
    (tmp_path / "company" / "services" / "__init__.py").write_text("")
    (tmp_path / "legacy" / "urls.py").write_text("")
    shutil.copy(ROOT / ".importlinter", tmp_path / ".importlinter")

    result = subprocess.run([_lint_imports(), "--no-cache"], cwd=tmp_path, env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=120)
    output = " ".join(result.stdout.split())
    assert result.returncode == 1, result.stdout + result.stderr
    for source, target in BROKEN:
        assert f"{source}._probe_{target.replace('.', '_')} -> {target}" in output, (source, target, result.stdout)
    for source, target in ALLOWED:
        assert f"{source}._probe_{target.replace('.', '_')} -> {target}" not in output, (source, target)
