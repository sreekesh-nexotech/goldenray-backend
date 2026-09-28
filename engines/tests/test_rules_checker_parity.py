"""Golden parity: engineering checker (PBC-*), battery compatibility, upgrade model, BOM lock, validation (ENG-*).

Every case of ``golden/rules_checker.json`` was produced by the real ``engineeringChecker.js`` /
``engineeringValidation.js`` / ``bomLock.js`` / ``batteryCompatibility.js`` / ``upgradeModel.js`` /
``validate-bom.mjs``; the Python result must equal it exactly.
"""

from __future__ import annotations

import pytest

from engines import bom_domain, engineering_checker
from engines.jscompat import UNDEFINED
from engines.tests.rules_golden import assert_error, assert_same, cases, frozen, ids, load

FILE = "rules_checker.json"
GOLDEN = load(FILE)
FX_CATALOG = frozen(GOLDEN["refs"]["fixtureCatalog"])
FX_MASTER = frozen(GOLDEN["refs"]["fixtureBatteryMaster"])
APPROVED = load("rules_catalog.json")["sections"]["catalog"][0]
APPROVED_CATALOG = frozen(APPROVED["catalog"])
APPROVED_MASTER = frozen(APPROVED["batteryMaster"])


def _check(arguments: dict, catalog, master):
    return engineering_checker.check_project_bom(
        bom=frozen(arguments.get("bom")),
        lines=frozen(arguments["lines"]) if arguments.get("lines") is not None else None,
        template_scope=arguments.get("templateScope", False),
        catalog=catalog,
        battery_master=master,
        at=arguments.get("at"),
        catalog_version=arguments.get("catalogVersion"),
        upgrade=frozen(arguments.get("upgrade")),
        future_upgrade=arguments.get("futureUpgrade", False),
    )


class TestRegister:
    def test_the_rule_register_is_the_javascript_register(self):
        header = GOLDEN["header"]
        assert header["rulesVersion"] == engineering_checker.RULES_VERSION == engineering_checker.DEFAULT_RULE_SET.version
        assert header["checkerRuleCount"] == len(engineering_checker.DEFAULT_RULE_SET.rules) == 35
        for js, rule in zip(header["checkerRules"], engineering_checker.DEFAULT_RULE_SET.rules, strict=True):
            assert (js["id"], js["category"], js["severity"], js["description"], js["inputs"], js["source"]) == (
                rule.code,
                rule.category,
                rule.severity.legacy,
                rule.description,
                list(rule.inputs),
                rule.source,
            )
        assert header["warningsRequiringAcknowledgement"] == list(bom_domain.warnings_requiring_acknowledgement())

    def test_the_validation_register_is_the_javascript_register(self):
        header = GOLDEN["header"]
        assert header["validationRuleCount"] == len(engineering_checker.VALIDATION_RULE_SET.rules) == 30
        for js, rule in zip(header["validationRules"], engineering_checker.VALIDATION_RULE_SET.rules, strict=True):
            assert (js["id"], js["severity"], js["description"], js["inputs"], js["source"]) == (rule.code, rule.severity.legacy, rule.description, list(rule.inputs), rule.source)

    def test_approved_upgrade_paths(self):
        assert_same(GOLDEN["header"]["approvedUpgradePaths"], engineering_checker.APPROVED_UPGRADE_PATHS)

    def test_every_rule_has_a_passing_and_a_failing_fixture(self):
        pbc = cases(FILE, "pbc")
        for code in engineering_checker.DEFAULT_RULE_SET.codes:
            assert any(case["rule"] == code and case["expect"] == "fail" for case in pbc), code
            assert any(case["rule"] == code and case["expect"] == "pass" for case in pbc), code
        validation = cases(FILE, "validation")
        declared_only = set(GOLDEN["header"]["declaredOnlyValidationRules"])
        for code in engineering_checker.VALIDATION_RULE_SET.codes:
            if code in declared_only:
                assert not any(finding["ruleId"] == code for case in validation for finding in case["output"]["findings"])
                continue
            assert any(case["rule"] == code and case["expect"] == "fail" for case in validation), code
            assert any(case["rule"] == code and case["expect"] == "pass" for case in validation), code


@pytest.mark.parametrize("case", cases(FILE, "pbc"), ids=ids(cases(FILE, "pbc")))
def test_pbc_rule_fixture(case):
    result = _check(case["input"], FX_CATALOG, FX_MASTER)
    assert_same(case["output"], result)
    if case["rule"]:
        fired = any(finding.rule_id == case["rule"] for finding in result.findings)
        assert fired == (case["expect"] == "fail")


@pytest.mark.parametrize("case", cases(FILE, "packs"), ids=ids(cases(FILE, "packs")))
def test_every_approved_pack(case):
    assert case["input"]["catalogRef"] == "approved"
    assert_same(case["output"], _check(case["input"], APPROVED_CATALOG, APPROVED_MASTER))


def test_packs_cover_every_approved_combination():
    packs = cases(FILE, "packs")
    assert len(packs) == 54
    assert {case["id"].split("|")[0] for case in packs} == {"ongrid", "hybrid"}


@pytest.mark.parametrize("case", cases(FILE, "battery"), ids=ids(cases(FILE, "battery")))
def test_battery(case):
    data = case["input"]
    if case["fn"] == "toBatteryMaster":
        result = engineering_checker.to_battery_master(frozen(data["item"]), frozen(data["overlay"]))
    elif case["fn"] == "resolveProtectionRequirement":
        result = engineering_checker.resolve_protection_requirement(frozen(data["battery"]))
    else:
        result = engineering_checker.check_battery_compatibility(frozen(data["battery"]), frozen(data["context"]))
    assert_same(case["output"], result)


@pytest.mark.parametrize("case", cases(FILE, "upgrade"), ids=ids(cases(FILE, "upgrade")))
def test_upgrade_model(case):
    data = case["input"]
    if case["fn"] == "buildUpgradeIdentity":
        result = engineering_checker.build_upgrade_identity(from_kw=data["fromKw"], to_kw=data["toKw"], tier=data["tier"], sections=frozen(data["sections"]))
    elif case["fn"] == "upgradeIdentityKey":
        result = engineering_checker.upgrade_identity_key(frozen(data["identity"]))
    else:
        result = engineering_checker.check_upgrade_section_policy(frozen(data["sections"]))
    assert_same(case["output"], result)


@pytest.mark.parametrize("case", cases(FILE, "lock"), ids=ids(cases(FILE, "lock")))
def test_lock(case):
    data = case["input"]

    def run():
        return bom_domain.attempt_lock(
            bom=frozen(data.get("bom")),
            catalog=FX_CATALOG,
            battery_master=FX_MASTER,
            catalog_version=data.get("catalogVersion"),
            locked_by=data.get("lockedBy"),
            locked_at=data.get("lockedAt"),
            acknowledgements=frozen(data.get("acknowledgements", [])),
            price_lookup=frozen(data.get("priceLookup", {})),
            upgrade=frozen(data.get("upgrade")),
            future_upgrade=data.get("futureUpgrade", False),
        )

    if "error" in case:
        with pytest.raises(bom_domain.BomError) as caught:
            run()
        assert_error(case["error"], caught.value)
    else:
        assert_same(case["output"], run())


@pytest.mark.parametrize("case", cases(FILE, "lockSummary"), ids=ids(cases(FILE, "lockSummary")))
def test_lock_summary(case):
    validation = case["input"]["validation"]
    result = None
    if validation is not None:
        # rebuild the verdict the summary reads from its counts
        counts = validation["counts"]
        result = engineering_checker.CheckResult(
            engineering_checker.CheckStatus(validation["status"]),
            (),
            engineering_checker.Counts(counts["blocked"], counts["warning"], counts["info"], counts["checksRun"]),
            validation["rulesVersion"],
            None,
            None,
            "",
        )
    assert_same(case["output"], bom_domain.lock_summary(result))


@pytest.mark.parametrize("case", cases(FILE, "validation"), ids=ids(cases(FILE, "validation")))
def test_validation_rule_fixture(case):
    result = engineering_checker.validate_engineering(frozen(case["input"]))
    assert_same(case["output"], result)
    if case["rule"]:
        assert any(finding.rule_id == case["rule"] for finding in result.findings) == (case["expect"] == "fail")


@pytest.mark.parametrize("case", cases(FILE, "consistency"), ids=ids(cases(FILE, "consistency")))
def test_battery_consistency(case):
    data = case["input"]
    assert_same(case["output"], engineering_checker.validate_battery_consistency(frozen(data["bom"]), frozen(data["profile"]), frozen(data["opts"])))


@pytest.mark.parametrize("section", ["pbc", "packs"])
def test_javascript_calling_convention_and_legacy_round_trip(section):
    """``check_project_bom_js`` (the callable engines-commercial's registry injects) returns the JavaScript object, and
    a stored JavaScript verdict parses back into a ``CheckResult`` (historic runs import losslessly)."""
    for case in cases(FILE, section):
        catalog, master = (APPROVED_CATALOG, APPROVED_MASTER) if section == "packs" else (FX_CATALOG, FX_MASTER)
        arguments = {**frozen(case["input"]), "catalog": catalog, "batteryMaster": master}
        assert_same(case["output"], engineering_checker.check_project_bom_js(arguments))
        parsed = engineering_checker.CheckResult.from_legacy(frozen(case["output"]))
        assert_same(case["output"], parsed)
        assert parsed.result == engineering_checker.CheckStatus(case["output"]["status"]).result


def test_undefined_detail_is_omitted():
    finding = engineering_checker.ValidationFinding("ENG-SYS-001", engineering_checker.Severity.BLOCK, "d", "m", UNDEFINED, "s")
    assert "detail" not in finding.as_dict()
