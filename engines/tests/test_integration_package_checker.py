"""Integration of two engine packages: engines-commercial's ``package_registry.run_package_checker`` with the real
engineering checker of engines-rules (``engineering_checker.check_project_bom_js``) injected.

The commercial package could only test the injection with a stub; the rules goldens hold every approved pack of
``data/pack-config.json`` checked at template scope, and ``generate_rules.mjs`` verified in JavaScript that
``packageApproval.runPackageChecker`` returns exactly that verdict (plus ``revisionNumber``/``architecture``/
``architectureSource``). Replaying the same packs through the Python pair must give the JavaScript's verdicts.
"""

from __future__ import annotations

import pytest

from engines import package_registry
from engines.bom_domain import CATEGORY_TO_ROLE, ENPHASE_COMPONENT_ROLES
from engines.engineering_checker import check_project_bom_js
from engines.frozen import thaw
from engines.tests.rules_golden import assert_same, cases, ids, load

PACKS = cases("rules_checker.json", "packs")
APPROVED = load("rules_catalog.json")["sections"]["catalog"][0]
CATALOG = thaw(APPROVED["catalog"])
BATTERY_MASTER = thaw(APPROVED["batteryMaster"])
ACTOR = {"userId": "PACK_PUBLISH", "role": "PROJECT_HEAD"}


def _category_of(component_id: str) -> str:
    found = [key for key, category in CATALOG["categories"].items() if any(item.get("id") == component_id for item in category.get("items") or [])]
    assert len(found) == 1, (component_id, found)
    return found[0]


def _package(case: dict) -> dict:
    """The registry package the JavaScript checked: its components carry the catalogue category as ``role``."""
    bom = case["input"]["bom"]
    components = [{"role": _category_of(line["componentId"]), "componentId": line["componentId"], "quantity": line["quantity"]} for line in case["input"]["lines"]]
    for component, line in zip(components, case["input"]["lines"], strict=True):
        assert (ENPHASE_COMPONENT_ROLES.get(component["componentId"]) or CATEGORY_TO_ROLE.get(component["role"]) or "OTHER") == line["role"]
    system_type, size, phase, tier, _ = case["id"].split("|")
    return {"packageId": "PKG-G", "systemType": system_type, "size": size, "phase": phase, "tier": tier, "architecture": bom["architecture"], "components": components, "revisionNumber": 1}


@pytest.mark.parametrize("case", PACKS, ids=ids(PACKS))
def test_run_package_checker_with_the_real_checker_matches_the_javascript(case):
    registry = {"packages": [_package(case)]}
    env = {"catalog": CATALOG, "batteryMaster": BATTERY_MASTER, "catalogVersion": case["input"]["catalogVersion"]}
    validation = package_registry.run_package_checker(registry, env, actor=ACTOR, package_id="PKG-G", at=case["input"]["at"], check_project_bom=check_project_bom_js)
    assert (validation["revisionNumber"], validation["architecture"], validation["architectureSource"]) == (1, case["input"]["bom"]["architecture"], "PACKAGE_DECLARED")
    assert registry["packages"][0]["lastValidation"] is validation
    verdict = {key: value for key, value in validation.items() if key not in ("revisionNumber", "architecture", "architectureSource")}
    assert_same(case["output"], verdict)


def test_every_approved_pack_is_replayed():
    assert len(PACKS) == 54
