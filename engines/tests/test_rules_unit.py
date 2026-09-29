"""engines-rules behaviour beyond the golden files: rule sets as data, frozen documents, JavaScript value semantics,
the gate's hard refusal, pure stores, and the platform decisions (documented in docs/decisions/engines-rules.md)."""

from __future__ import annotations

import copy
import pickle
from decimal import Decimal
from enum import StrEnum

import pytest

from engines import bom_domain, content_fit, engineering_checker, gate, quotation_payload
from engines.engineering_checker import DEFAULT_RULE_SET, VALIDATION_RULE_SET, RuleSet, RuleSetError, Severity
from engines.frozen import FrozenDict, deep_freeze, is_frozen, jsonable, sha256_hex, thaw, to_json
from engines.jscompat import (
    UNDEFINED,
    coalesce,
    format_en_in,
    is_nullish,
    iso_from_ms,
    js_array,
    js_finite_number,
    js_keys,
    js_number,
    js_round,
    js_string,
    js_trim,
    js_truthy,
    number_text,
    parse_iso_ms,
    prop,
    round_places,
    to_fixed,
    utf16_len,
)
from engines.tests.rules_golden import cases, frozen, load

FX = load("rules_checker.json")["refs"]
FX_CATALOG, FX_MASTER = frozen(FX["fixtureCatalog"]), frozen(FX["fixtureBatteryMaster"])
PBC = {case["id"]: case for case in cases("rules_checker.json", "pbc")}


def _check(case_id: str, rule_set: RuleSet | None = None):
    data = PBC[case_id]["input"]
    return engineering_checker.check_project_bom(
        bom=frozen(data.get("bom")),
        lines=frozen(data.get("lines")) if data.get("lines") is not None else None,
        catalog=FX_CATALOG,
        battery_master=FX_MASTER,
        at=data["at"],
        catalog_version=data["catalogVersion"],
        upgrade=frozen(data.get("upgrade")),
        future_upgrade=data.get("futureUpgrade", False),
        template_scope=data.get("templateScope", False),
        rule_set=rule_set,
    )


# ---------------------------------------------------------------------------------------------------------------------
# Rule sets as data
# ---------------------------------------------------------------------------------------------------------------------


class TestRuleSetDocument:
    @pytest.mark.parametrize("rule_set", [DEFAULT_RULE_SET, VALIDATION_RULE_SET], ids=["checker", "validation"])
    def test_round_trip(self, rule_set):
        document = rule_set.as_json()
        assert RuleSet.from_json(document) == rule_set
        assert RuleSet.from_json(deep_freeze(document)) == rule_set
        assert to_json(document)  # JSONB-ready: plain containers, int/float numbers, strings

    def test_default_document_shape(self):
        document = DEFAULT_RULE_SET.as_json()
        assert (document["engine"], document["version"], len(document["rules"])) == ("engineeringChecker", "phase1e.1", 35)
        d002 = next(rule for rule in document["rules"] if rule["code"] == "PBC-D-002")
        assert d002 == {
            "code": "PBC-D-002",
            "category": "INVERTER_ELECTRICAL_LIMITS",
            "severity": "BLOCK",
            "description": "Single-panel Voc at minimum cell temperature already exceeds the inverter maximum input voltage",
            "inputs": ["panel.voc", "panel.tempCoeffVoc", "inverter.maxInputVoltage"],
            "source": "App.jsx stringConfig — Kerala minimum cell temperature 15 °C, STC 25 °C",
            "requiresAcknowledgement": False,
            "params": {"minCellTemperatureC": 15, "stcTemperatureC": 25},
        }
        assert {rule["severity"] for rule in document["rules"]} == {"BLOCK", "WARN"}
        assert [rule["code"] for rule in document["rules"] if rule["requiresAcknowledgement"]] == ["PBC-D-001", "PBC-D-003", "PBC-F-002", "PBC-G-002", "PBC-N-001", "PBC-R-002"]

    def test_missing_optional_members_and_params_take_the_defaults(self):
        document = DEFAULT_RULE_SET.as_json()
        for rule in document["rules"]:
            for key in ("category", "description", "inputs", "source", "requiresAcknowledgement", "params"):
                rule.pop(key)
        assert RuleSet.from_json(document) == DEFAULT_RULE_SET

    def test_legacy_severity_words_are_accepted(self):
        document = DEFAULT_RULE_SET.as_json()
        for rule in document["rules"]:
            rule["severity"] = Severity(rule["severity"]).legacy
        assert RuleSet.from_json(document) == DEFAULT_RULE_SET

    @pytest.mark.parametrize(
        ("mutate", "path", "message"),
        [
            (lambda d: d.update(version=""), "version", "1–16"),
            (lambda d: d.update(version="x" * 17), "version", "1–16"),
            (lambda d: d["rules"].append("nope"), "rules[35]", "object"),
            (lambda d: d["rules"].append({"code": "PBC-Z-001"}), "rules[35].code", "unknown rule"),
            (lambda d: d["rules"].append(dict(d["rules"][0])), "rules[35].code", "twice"),
            (lambda d: d["rules"][0].update(severity="FATAL"), "rules[0].severity", "BLOCK, WARN or INFO"),
            (lambda d: d["rules"][0].update(category="OTHER"), "rules[0].category", "SYSTEM_ARCHITECTURE"),
            (lambda d: d["rules"][0].update(description=""), "rules[0].description", "non-empty"),
            (lambda d: d["rules"][0].update(inputs="bom"), "rules[0].inputs", "list"),
            (lambda d: d["rules"][0].update(requiresAcknowledgement="yes"), "rules[0].requiresAcknowledgement", "true or false"),
            (lambda d: d["rules"][0].update(params=[]), "rules[0].params", "object"),
            (lambda d: d["rules"][0].update(params={"x": 1}), "rules[0].params.x", "unknown parameter"),
            (lambda d: d["rules"][6]["params"].update(minCellTemperatureC="cold"), "rules[6].params.minCellTemperatureC", "a number"),
            (lambda d: d["rules"][19]["params"].update(requiredRoles=["PANEL", "ROOF"]), "rules[19].params.requiredRoles", "BOM roles"),
            (lambda d: d["rules"][19]["params"].update(architectureRoles={"ENPHASE": "MICRO"}), "rules[19].params.architectureRoles", "role lists"),
            (lambda d: d["rules"][25]["params"].update(batteryQuantity=0), "rules[25].params.batteryQuantity", "positive"),
            (lambda d: d["rules"][29]["params"].update(isolatorByPhase={"3P": ""}), "rules[29].params.isolatorByPhase", "strings"),
            (lambda d: d["rules"][32]["params"].update(approvedUpgradePaths=[]), "rules[32].params.approvedUpgradePaths", "non-empty"),
            (lambda d: d["rules"][32]["params"].update(approvedUpgradePaths=[{"id": "U", "fromKw": 3, "toKw": 5, "explicitBom": "yes"}]), "rules[32].params.approvedUpgradePaths", "{id"),
            (lambda d: d["rules"][32]["params"].update(approvedUpgradePaths=[{"id": "U", "fromKw": 3, "toKw": 5, "explicitBom": True, "note": 5}]), "rules[32].params.approvedUpgradePaths", "{id"),
            (lambda d: d["rules"][32]["params"].update(approvedUpgradePaths=["U"]), "rules[32].params.approvedUpgradePaths", "{id"),
            (lambda d: d["rules"].pop(), "rules", "missing rule(s): PBC-S-001"),
        ],
    )
    def test_invalid_documents_list_every_problem(self, mutate, path, message):
        document = DEFAULT_RULE_SET.as_json()
        mutate(document)
        with pytest.raises(RuleSetError) as caught:
            RuleSet.from_json(document)
        assert caught.value.code == "rule_set_invalid"
        assert any(error["path"] == path and message in error["message"] for error in caught.value.errors), caught.value.errors

    @pytest.mark.parametrize("document", [None, [], {"engine": "other"}, {"engine": "engineeringChecker", "version": "v", "rules": {}}])
    def test_unusable_documents(self, document):
        with pytest.raises(RuleSetError):
            RuleSet.from_json(document)

    def test_rules_follow_the_register_order_whatever_the_stored_order(self):
        document = DEFAULT_RULE_SET.as_json()
        document["rules"].reverse()
        assert RuleSet.from_json(document).codes == DEFAULT_RULE_SET.codes

    def test_engines_refuse_the_other_engines_rule_set(self):
        with pytest.raises(RuleSetError):
            engineering_checker.check_project_bom(bom=None, rule_set=VALIDATION_RULE_SET)
        with pytest.raises(RuleSetError):
            engineering_checker.validate_engineering({}, rule_set=DEFAULT_RULE_SET)


class TestRuleSetParametersDriveTheChecker:
    def test_severity_override(self):
        rules = DEFAULT_RULE_SET.with_changes(version="t.1", severities={"PBC-D-003": "INFO", "PBC-N-001": "INFO"})
        result = _check("D-003 panel and inverter", rules)
        assert result.status == engineering_checker.CheckStatus.VALID and result.result == engineering_checker.RunResult.PASS
        assert result.counts.info == 2 and result.rules_version == "t.1"
        assert result.findings[0].as_dict()["severity"] == "INFO" and result.findings[0].as_row()["severity"] == "INFO"

    def test_cold_temperature_parameter(self):
        default = _check("D-002 cold Voc 50.8 V")
        assert not any(finding.rule_id == "PBC-D-002" for finding in default.findings)
        frozen_winter = DEFAULT_RULE_SET.with_changes(version="t.2", params={"PBC-D-002": {"minCellTemperatureC": -10000}})
        finding = next(finding for finding in _check("D-002 cold Voc 50.8 V", frozen_winter).findings if finding.rule_id == "PBC-D-002")
        assert "at -10000 °C" in finding.message and "minimum cell temperature -10000 °C" in finding.reason

    def test_isolator_parameter(self):
        rules = DEFAULT_RULE_SET.with_changes(version="t.3", params={"PBC-P-001": {"isolatorByPhase": {"1P": "fx_iso", "3P": "is2"}, "decisionByPhase": {"1P": "site decision"}}})
        assert any(finding.rule_id == "PBC-P-001" and finding.reason == "site decision" for finding in _check("P-001 is1 on 1P", rules).findings)
        assert not any(finding.rule_id == "PBC-P-001" for finding in _check("P-001 other isolator on 1P", rules).findings)

    def test_upgrade_paths_and_battery_quantity_parameters(self):
        rules = DEFAULT_RULE_SET.with_changes(
            version="t.4",
            params={"PBC-R-001": {"approvedUpgradePaths": [{"id": "UPG-3-7", "fromKw": 3, "toKw": 7, "explicitBom": False, "note": "trial"}]}, "PBC-M-002": {"batteryQuantity": 2}},
        )
        upgrade = next(finding for finding in _check("R-001 unapproved 3→7", rules).findings if finding.rule_id.startswith("PBC-R"))
        assert (upgrade.rule_id, upgrade.reason) == ("PBC-R-002", "trial")
        battery = next(finding for finding in _check("M-002 one battery", rules).findings if finding.rule_id == "PBC-M-002")
        assert battery.reason == "D12 — the V1 Enphase package is 2 FlexPhase batteries."

    def test_prohibited_roles_and_required_roles(self):
        rules = DEFAULT_RULE_SET.with_changes(
            version="t.5", params={"PBC-P-002": {"prohibitedRoles": ["DC_ISOLATOR", "MONITORING"]}, "PBC-K-001": {"requiredRoles": ["PANEL", "STRUCTURE", "AC_ISOLATOR", "METER"]}}
        )
        findings = _check("A-002 Enphase part on Deye", rules).findings
        assert any(finding.rule_id == "PBC-P-002" and finding.message == "The BOM contains a MONITORING role." for finding in findings)
        assert any(finding.rule_id == "PBC-K-001" and "METER" in finding.message for finding in findings)

    def test_acknowledgements_follow_the_rule_set(self):
        bom = frozen(PBC["D-003 panel and inverter"]["input"]["bom"])
        strict = bom_domain.attempt_lock(bom=bom, catalog=FX_CATALOG, battery_master=FX_MASTER, locked_at="t")
        assert strict.result == bom_domain.LockResult.REJECTED_UNACKNOWLEDGED
        document = DEFAULT_RULE_SET.as_json()
        for rule in document["rules"]:
            rule["requiresAcknowledgement"] = False
        relaxed = RuleSet.from_json({**document, "version": "t.6"})
        outcome = bom_domain.attempt_lock(bom=bom, catalog=FX_CATALOG, battery_master=FX_MASTER, locked_at="t", rule_set=relaxed)
        assert outcome.result == bom_domain.LockResult.LOCKED and outcome.snapshot["rulesVersion"] == "t.6"
        assert bom_domain.warnings_requiring_acknowledgement(relaxed) == ()
        assert bom_domain.requires_acknowledgement("PBC-D-003") and not bom_domain.requires_acknowledgement("PBC-D-003", relaxed)

    def test_validation_parameters(self):
        rules = VALIDATION_RULE_SET.with_changes(version="v.1", params={"ENG-PNL-001": {"subsidyTypes": ["commercial"]}}, severities={"ENG-CMP-005": "BLOCK"})
        result = engineering_checker.validate_engineering(
            {"bom": {"bomLines": [{"name": "Panel", "qty": 1, "category": "panel", "panelType": "NON_DCR", "autoSelected": True}]}, "subsidyType": "commercial"}, rule_set=rules
        )
        assert [finding.rule_id for finding in result.findings] == ["ENG-CMP-005", "ENG-PNL-001"]
        assert result.status == engineering_checker.CheckStatus.BLOCKED and result.blocked == 2


class TestCheckerResultShapes:
    def test_rows_summary_and_views(self):
        result = _check("B-002 panel 600 V below inverter 1000 V")
        assert result.result == engineering_checker.RunResult.FAIL
        assert [finding.rule_id for finding in result.blockers] == ["PBC-B-002"]
        assert {finding.rule_id for finding in result.warnings} == {"PBC-D-003", "PBC-N-001"}
        row = result.blockers[0].as_row()
        assert row["rule_code"] == "PBC-B-002" and row["severity"] == "BLOCK" and row["context"]["componentIds"] == ["fx_inv_1p", "fx_pnl_lowsys"]
        summary = result.summary()
        assert summary["status"] == "BLOCKED" and summary["deterministicKey"] == result.deterministic_key and summary["counts"]["checksRun"] == 35

    def test_determinism_ignores_line_order_and_at(self):
        data = PBC["B-002 panel 600 V below inverter 1000 V"]["input"]
        bom = frozen(data["bom"])
        lines = list(bom_domain.effective_lines(bom))
        first = engineering_checker.check_project_bom(bom=bom, lines=lines, catalog=FX_CATALOG, at="a")
        second = engineering_checker.check_project_bom(bom=bom, lines=list(reversed(lines)), catalog=FX_CATALOG, at="b")
        assert first.deterministic_key == second.deterministic_key

    def test_severity_and_status_words(self):
        assert Severity.parse("BLOCKED") is Severity.BLOCK and Severity.parse("WARN") is Severity.WARN
        assert [status.result.value for status in engineering_checker.CheckStatus] == ["PASS", "WARN", "FAIL"]
        with pytest.raises(ValueError):
            Severity.parse("LOUD")

    def test_registry_lines(self):
        lines = engineering_checker.registry_lines(
            [{"role": "panel", "componentId": "p2", "quantity": 6}, {"role": "enphase", "componentId": "en7", "quantity": 1}, {"role": "meter_box", "componentId": "mb", "quantity": 1}]
        )
        assert [line["role"] for line in lines] == ["ENERGY_SYSTEM_CONTROLLER", "OTHER", "PANEL"]  # runPackageChecker sorts by role

    def test_battery_values_compare_the_javascript_way(self):
        # string window: JavaScript compares two strings lexicographically ("51.2" ≥ "40", "51.2" ≤ "60")
        battery = engineering_checker.to_battery_master({"id": "b"}, {"nominalVoltage": "51.2", "compatiblePhases": ["1P"]})
        check = engineering_checker.check_battery_compatibility(battery, {"inverter": {"id": "i", "batteryVoltageMin": "40", "batteryVoltageMax": "60"}, "quantity": "two"})
        results = {item["id"]: item for item in check["checks"]}
        assert results["BC-C"]["result"] == "PASS"
        assert results["BC-I"] == {"id": "BC-I", "name": "Battery quantity", "result": "PASS", "message": "Quantity two."}


# ---------------------------------------------------------------------------------------------------------------------
# Frozen documents
# ---------------------------------------------------------------------------------------------------------------------


class Colour(StrEnum):
    RED = "RED"


class TestFrozen:
    def test_every_mutation_is_refused(self):
        document = deep_freeze({"a": {"b": [1, {"c": 2}]}})
        for mutate in (
            lambda: document.__setitem__("a", 1),
            lambda: document.__delitem__("a"),
            lambda: document.update(a=1),
            lambda: document.pop("a"),
            lambda: document.popitem(),
            lambda: document.clear(),
            lambda: document.setdefault("x", 1),
            lambda: document["a"].__setitem__("b", 1),
        ):
            with pytest.raises(TypeError):
                mutate()
        with pytest.raises(TypeError):
            document |= {"x": 1}
        with pytest.raises(AttributeError):
            document["a"]["b"].append(3)
        assert is_frozen(document) and not is_frozen({"a": 1}) and not is_frozen([1])

    def test_copies_pickles_and_repr(self):
        document = deep_freeze({"a": [Decimal("1.5")]})
        assert copy.copy(document) is document and copy.deepcopy(document) is document
        restored = pickle.loads(pickle.dumps(document))
        assert restored == document and isinstance(restored, FrozenDict)
        assert repr(document).startswith("FrozenDict(")
        assert pickle.loads(pickle.dumps(UNDEFINED)) is UNDEFINED and repr(UNDEFINED) == "UNDEFINED" and not UNDEFINED

    def test_json_semantics(self):
        class Shape:
            def as_dict(self):
                return {"kind": Colour.RED}

        value = deep_freeze({"f": 0.1, "nan": float("nan"), "inf": float("inf"), "d": Decimal("NaN"), "gone": UNDEFINED, "list": [UNDEFINED, 1], "shape": Shape(), Colour.RED: Colour.RED})
        assert value == {"f": Decimal("0.1"), "nan": None, "inf": None, "d": None, "list": (None, 1), "shape": {"kind": "RED"}, "RED": "RED"}
        assert type(value["shape"]["kind"]) is str and type(next(iter(value))) is str
        assert deep_freeze(UNDEFINED) is None
        with pytest.raises(TypeError):
            deep_freeze({1: "x"})
        with pytest.raises(TypeError):
            deep_freeze({"s": {1, 2}})

    def test_thaw_and_canonical_json(self):
        document = deep_freeze({"b": [1, Decimal("2.50"), 1e21], "a": {"é": "മ", "x": None, "t": True, "f": False}})
        assert thaw(document) == {"b": [1, Decimal("2.50"), Decimal("1E+21")], "a": {"é": "മ", "x": None, "t": True, "f": False}}
        assert type(thaw(document)["a"]) is dict and type(thaw(document)["b"]) is list
        assert to_json(document) == '{"b":[1,2.5,1e+21],"a":{"é":"മ","x":null,"t":true,"f":false}}'
        assert to_json(document, sort_keys=True).startswith('{"a":')
        assert sha256_hex(document) == sha256_hex(deep_freeze({"a": document["a"], "b": document["b"]}))
        assert to_json({"n": float("nan"), "u": UNDEFINED, "l": [UNDEFINED]}) == '{"n":null,"l":[null]}'

        class Shape:
            def as_dict(self):
                return {"x": 1}

        assert to_json([Shape()]) == '[{"x":1}]'
        assert jsonable(deep_freeze({"a": [Decimal("229000.0"), Decimal("2.50"), Decimal("NaN")], "u": UNDEFINED})) == {"a": [229000, 2.5, None]}
        assert jsonable({"u": UNDEFINED, "l": [UNDEFINED, "x", True]}) == {"l": [None, "x", True]}
        with pytest.raises(TypeError):
            to_json({"s": {1}})


# ---------------------------------------------------------------------------------------------------------------------
# JavaScript value semantics
# ---------------------------------------------------------------------------------------------------------------------


class TestJsValues:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "0"),
            (True, "1"),
            (False, "0"),
            (UNDEFINED, "NaN"),
            (3, "3"),
            (0.1, "0.1"),
            ("  12.5\n", "12.5"),
            ("", "0"),
            ("﻿ 7 　", "7"),
            ("Infinity", "Infinity"),
            ("+Infinity", "Infinity"),
            ("-Infinity", "-Infinity"),
            ("0x1F", "31"),
            ("0o17", "15"),
            ("0b101", "5"),
            ("1e3", "1000"),
            (".5", "0.5"),
            ("5.", "5"),
            ("12px", "NaN"),
            ([], "0"),
            ([7], "7"),
            ([1, 2], "NaN"),
            ({}, "NaN"),
        ],
    )
    def test_number(self, value, expected):
        assert number_text(js_number(value)) == expected

    def test_finite_number_and_truthiness(self):
        assert [js_finite_number(value) for value in (None, UNDEFINED, "", "x", "Infinity", "4")] == [None, None, None, None, None, Decimal(4)]
        assert [js_truthy(value) for value in (None, UNDEFINED, False, 0, Decimal("0.0"), float("nan"), "", "0", [], {}, True, 2)] == [False] * 7 + [True] * 5

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("NaN"), "NaN"),
            (Decimal("-Infinity"), "-Infinity"),
            (Decimal("-0"), "0"),
            (Decimal("229000.00"), "229000"),
            (Decimal("0.000001"), "0.000001"),
            (Decimal("0.0000001"), "1e-7"),
            (Decimal("-1.25E-7"), "-1.25e-7"),
            (Decimal("1E+21"), "1e+21"),
            (Decimal("12345678901234567890"), "12345678901234567890"),
        ],
    )
    def test_number_text(self, value, expected):
        assert number_text(value) == expected

    def test_string(self):
        assert [js_string(value) for value in (None, UNDEFINED, True, False, "x", 1.5, Decimal("2.50"), [1, None, [2, 3]], (UNDEFINED,), {"a": 1})] == [
            "null",
            "undefined",
            "true",
            "false",
            "x",
            "1.5",
            "2.5",
            "1,,2,3",
            "",
            "[object Object]",
        ]

    def test_rounding(self):
        assert [js_round(Decimal(value)) for value in ("2.5", "-2.5", "-2.6", "0.49999")] == [3, -2, -3, 0]
        assert round_places(Decimal("1.005"), 2) == Decimal("1.01")
        assert [to_fixed(Decimal(value), 1) for value in ("50.8365", "1004.25", "-0.04", "-0", "3")] == ["50.8", "1004.3", "-0.0", "0.0", "3.0"]

    def test_text_helpers(self):
        assert utf16_len("a😀മ") == 4
        assert js_trim("  x  ") == "x" and js_trim("\x1cx") == "\x1cx"
        assert coalesce(None, UNDEFINED, 0, 1) == 0 and coalesce(None, UNDEFINED) is UNDEFINED
        assert is_nullish(None) and is_nullish(UNDEFINED) and not is_nullish(0)
        assert js_keys({"10": 1, "b": 2, "2": 3, "01": 4, "4294967295": 5}) == ["2", "10", "b", "01", "4294967295"] and js_keys([1]) == []
        assert prop({"a": 1}, "a") == 1 and prop({"a": 1}, "b") is UNDEFINED and prop("text", "length") is UNDEFINED
        assert js_array((1, 2)) == [1, 2] and js_array({"a": 1}) is None

    @pytest.mark.parametrize(
        ("text", "iso"),
        [
            ("2026-09-14T07:56:28.682Z", "2026-09-14T07:56:28.682Z"),
            ("2026-09-01", "2026-09-01T00:00:00.000Z"),
            ("2026-09-01T05:30", "2026-09-01T05:30:00.000Z"),
            ("2026-09-01T05:30:00+05:30", "2026-09-01T00:00:00.000Z"),
            ("2026-09-01T00:00:00.1234567-01:00", "2026-09-01T01:00:00.123Z"),
            ("2026-09-01T24:00:00", "2026-09-02T00:00:00.000Z"),
            ("1969-12-31T23:59:59.999Z", "1969-12-31T23:59:59.999Z"),
        ],
    )
    def test_iso_dates(self, text, iso):
        assert iso_from_ms(parse_iso_ms(text)) == iso

    @pytest.mark.parametrize("text", ["2026-02-30", "2026-09-01T25:00", "2026-09-01T24:00:01", "September 1, 2026", "", None, 1_700_000_000_000, "2026-09-01T10:61"])
    def test_non_iso_dates_are_refused(self, text):
        assert parse_iso_ms(text) is None

    @pytest.mark.parametrize(
        ("value", "text"),
        [(0, "0"), (999, "999"), (1000, "1,000"), (100000, "1,00,000"), (12345678, "1,23,45,678"), (-2400, "-2,400"), (Decimal("1234.5678"), "1,234.568"), (Decimal("2.10"), "2.1")],
    )
    def test_indian_grouping(self, value, text):
        assert format_en_in(value) == text


# ---------------------------------------------------------------------------------------------------------------------
# Gate, BOM domain, payload, content: platform-facing behaviour
# ---------------------------------------------------------------------------------------------------------------------


class TestGateRefusal:
    def test_blocked_gate_raises_with_every_failed_check(self):
        report = gate.evaluate_generation_gate()
        with pytest.raises(gate.GenerationBlocked) as caught:
            report.raise_if_blocked()
        error = caught.value
        assert error.code == "GENERATION_BLOCKED" and len(error.failed_checks) == 8 and error.report is report
        assert error.message.startswith("Quotation generation disabled. Failed checks: CUSTOMER_DATA, SYSTEM_CONFIGURATION")
        assert len(error.reasons) == 8 and error.reasons[0].startswith("CUSTOMER_DATA: ")

    def test_passing_gate_does_not_raise(self):
        case = next(item for item in cases("rules_gate.json", "gate") if item["id"] == "all pass")
        data = case["input"]
        report = gate.evaluate_generation_gate(
            customer=frozen(data["customer"]),
            system=frozen(data["system"]),
            bom_snapshot=frozen(data["bomSnapshot"]),
            engineering=frozen(data["engineering"]),
            commercial_snapshot=frozen(data["commercialSnapshot"]),
            subsidy_treatment=data["subsidyTreatment"],
            quotation=frozen(data["quotation"]),
        )
        report.raise_if_blocked()
        assert report.passed and report.blocked_checks == () and report.reasons == ()


class TestBomDomain:
    def test_results_are_frozen_and_inputs_untouched(self):
        lines = [{"role": "PANEL", "componentId": "p", "quantity": 1}]
        bom = bom_domain.create_project_bom(project_id="P", lines=lines)
        edited = bom_domain.set_component(bom, role="PANEL", component_id="q")
        assert is_frozen(bom) and is_frozen(edited) and bom["overrides"] == {} and lines == [{"role": "PANEL", "componentId": "p", "quantity": 1}]
        with pytest.raises(bom_domain.BomError) as caught:
            bom_domain.set_component(None, role="PANEL", component_id="q")
        assert caught.value.code == "NO_PROJECT_BOM"

    def test_status_from_a_check_result(self):
        result = _check("A-001 declared")
        assert bom_domain.status_from_validation(result) == bom_domain.BomStatus.WARNING
        assert bom_domain.lock_summary(None) == {"blockers": 0, "warnings": 0, "canLock": False, "label": "Not validated"}


class TestPayloadAndContent:
    def test_payload_is_frozen_and_projection_keeps_full_readers_untouched(self):
        payload = quotation_payload.build_quotation_payload()
        with pytest.raises(TypeError):
            payload["pricing"] = None
        assert quotation_payload.project_issued_payload_for_actor(payload, True) is payload
        assert quotation_payload.project_issued_snapshot_for_actor({"a": 1}, True) == {"a": 1}
        assert quotation_payload.normalize_renderer_pinning(None, "t") is None
        assert quotation_payload.compare_recalculation(None, {}, {}) is None
        assert quotation_payload.with_alternatives(payload, []) is payload

    def test_recalculation_delta_is_exact(self):
        previous = frozen({"cost": {"totalActualCost": Decimal("180900.46")}, "pricing": {"sellingPriceBeforeGST": 100}, "versions": {"pricingEngineVersion": "p1", "moneyRuleVersion": "money.1"}})
        result = quotation_payload.compare_recalculation(previous, frozen({"totalActualCost": Decimal("190000.1")}), frozen({"sellingPriceBeforeGST": None, "pricingEngineVersion": "p2"}))
        assert result["totalActualCost"] == {"before": Decimal("180900.46"), "after": Decimal("190000.1"), "delta": Decimal("9099.64")}  # JavaScript: 9099.640000000014
        assert result["sellingPriceBeforeGST"]["delta"] is None
        assert result["versionsChanged"] == tuple(key for key in quotation_payload.VERSION_KEYS if key != "moneyRuleVersion")

    def test_unresolved_panel_subsidy_result(self):
        result = quotation_payload.panel_unresolved_subsidy_result("residential")
        assert result["status"] == "INVALID_INPUT" and result["available"] is False and result["subsidyType"] == "residential"

    def test_issued_at_is_never_guessed(self):
        # JavaScript reads new Date(null) as the epoch; the platform refuses a missing issuedAt instead.
        with pytest.raises(quotation_payload.PolicyError) as caught:
            quotation_payload.resolve_effective_validity({"validity": {"defaultDays": 15}}, None, None)
        assert caught.value.code == quotation_payload.PolicyErrorCode.VALIDITY_INVALID

    def test_content_store_functions_are_pure(self):
        published = frozen(load("rules_content.json")["refs"]["published"])
        store = content_fit.create_empty_store(published)
        saved = content_fit.save_draft(store, published, actor_id="ph", at="2026-09-20T10:00:00.000Z")
        assert store["draft"]["updatedAt"] is None and saved["draft"]["updatedAt"] == "2026-09-20T10:00:00.000Z"
        with pytest.raises(content_fit.ContentInvalid) as caught:
            content_fit.save_draft(store, {}, actor_id="ph", at="x")
        assert caught.value.code == "CONTENT_INVALID" and caught.value.errors
        published_store = content_fit.publish_draft(saved, actor_id="admin", at="2026-09-21T00:00:00.000Z")
        assert saved["published"]["version"] == 1 and published_store["published"]["version"] == 2
        assert content_fit.label(published, "missing") == "" and content_fit.pick({"en": " "}) == ""

    def test_branding_writes_are_pure(self):
        store = frozen(load("rules_content.json")["refs"]["brandingStore"])
        updated = content_fit.publish_branding_version(store, actor_id="admin", kind="upi", account_id="UPI-NEW", values={"upiId": "a@b"}, at="2026-09-20T10:00:00.000Z")
        assert "UPI-NEW" not in store["upi"]["accounts"] and "UPI-NEW" in updated["upi"]["accounts"]
        assert content_fit.demo_branding_kinds(None) == ()
        with pytest.raises(content_fit.BrandingError) as caught:
            content_fit.list_accounts(store, "logo")
        assert caught.value.code == "BRANDING_KIND_INVALID"
