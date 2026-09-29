"""engines-rules adversarial review: regressions for the defects found by differential fuzzing against the real
JavaScript (checker, fit guard, validation, policy, commercial snapshot) and by probing the rule-set document API.

Each test failed before its fix; the JavaScript result it pins was captured from the real Flarize modules."""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from engines import content_fit, engineering_checker, quotation_payload
from engines.engineering_checker import DEFAULT_RULE_SET, VALIDATION_RULE_SET, RuleSet, RuleSetError
from engines.jscompat import js_finite_number, js_keys, js_number, parse_iso_ms
from engines.tests.rules_golden import assert_same, cases, frozen, load

FX = load("rules_checker.json")["refs"]
FX_CATALOG, FX_MASTER = frozen(FX["fixtureCatalog"]), frozen(FX["fixtureBatteryMaster"])


def _panel_quantity_findings(quantity):
    result = engineering_checker.check_project_bom(
        bom={"phase": "1P", "architecture": "DEYE"},
        lines=[frozen({"role": "PANEL", "componentId": "fx_pnl", "quantity": quantity})],
        catalog=FX_CATALOG,
        battery_master=FX_MASTER,
    )
    return {finding.rule_id for finding in result.findings}


# ---------------------------------------------------------------------------------------------------------------------
# Number() and Date parsing: JavaScript digits are ASCII, and numbers are binary64
# ---------------------------------------------------------------------------------------------------------------------


class TestNumberParsingIsJavaScriptNumber:
    @pytest.mark.parametrize("text", ["൩", "٣", "３", "𝟑", "൧൨", "1൨", "१०"])
    def test_non_ascii_digits_are_not_a_number(self, text):
        # Number('൩') is NaN in JavaScript; Python's Decimal() would read the Malayalam digit as 3
        assert js_number(text).is_nan()
        assert js_finite_number(text) is None

    @pytest.mark.parametrize("text", ["൩", "３", "൧൨"])
    def test_a_quantity_in_non_ascii_digits_is_blocked_by_pbc_l_001(self, text):
        assert "PBC-L-001" in _panel_quantity_findings(text)

    @pytest.mark.parametrize(("text", "expected"), [("1e400", "Infinity"), ("-1e400", "-Infinity"), ("1e-400", "0"), ("-1e-400", "-0")])
    def test_values_outside_binary64_overflow_and_underflow_like_javascript(self, text, expected):
        assert js_number(text) == Decimal(expected) and js_number(text).is_signed() == expected.startswith("-")
        assert js_number(Decimal(text)) == Decimal(expected)

    @pytest.mark.parametrize("text", ["1e400", "1e-400"])
    def test_an_overflowing_or_underflowing_quantity_is_blocked_by_pbc_l_001(self, text):
        # JavaScript: Number('1e400') is Infinity (not finite → null), Number('1e-400') is 0 (≤ 0)
        assert "PBC-L-001" in _panel_quantity_findings(text)
        assert js_finite_number(text) in (None, Decimal(0))

    @pytest.mark.parametrize("text", ["1.7976931348623157e308", "5e-324", "0.1", "229000.10"])
    def test_representable_values_stay_exact(self, text):
        assert js_number(text) == Decimal(text)

    def test_non_ascii_digits_are_not_array_indexes(self):
        # Object.keys({b: 1, '1٢': 2, 2: 3}) is ['2', 'b', '1٢']: '1٢' is not an array index in JavaScript
        assert js_keys({"b": 1, "1٢": 2, "2": 3}) == ["2", "b", "1٢"]

    @pytest.mark.parametrize("text", ["２０２６-09-01", "൨൦൨൬-09-01", "2026-0൯-01", "2026-09-01T1൦:00:00Z"])
    def test_non_ascii_digits_are_not_iso_dates(self, text):
        assert parse_iso_ms(text) is None

    def test_a_validity_date_in_non_ascii_digits_is_refused(self):
        store = frozen({"policies": [{"policyId": "P1", "status": "ACTIVE", "validityDays": 15, "effectiveFrom": "2026-01-01", "effectiveTo": None}], "validity": {"defaultDays": 30}})
        with pytest.raises(quotation_payload.PolicyError) as caught:
            quotation_payload.resolve_effective_validity(store, None, "２０２６-09-10T10:00:00Z")
        assert caught.value.code == quotation_payload.PolicyErrorCode.VALIDITY_INVALID

    def test_the_fit_guard_refuses_a_quantity_in_malayalam_digits(self):
        content = copy.deepcopy(load("rules_content.json")["refs"]["published"])
        content["appliances"]["profiles"]["3"][0]["qty"] = "൩"
        # captured from quotationContent.validateContent
        expected = [{"path": 'appliances.profiles["3"][1].qty', "code": "INVALID", "message": 'appliances.profiles["3"][1] quantity must be 0–50'}]
        assert_same(expected, content_fit.validate_content(frozen(content)))


# ---------------------------------------------------------------------------------------------------------------------
# NaN compares false in JavaScript; a Decimal NaN ordering comparison raises
# ---------------------------------------------------------------------------------------------------------------------


class TestNaNQuantities:
    LINES = [
        {"name": "SEG. 100Ah", "qty": 1, "category": "battery", "itemId": "fx_bat_norating"},
        {"name": "160A MCCB", "qty": 1, "category": "mccb_box", "itemId": "mb1"},
        {"name": "25mm Battery Cable", "qty": 2, "category": "battery_cable", "itemId": "fx_bcable"},
    ]
    PROFILE = {"batteryIncluded": True, "batteryQuantity": "one"}

    def test_battery_consistency_with_a_non_numeric_profile_quantity(self):
        # validate-bom.mjs: Number('one') is NaN; NaN > 0 and NaN === 0 are both false → nothing to report
        result = engineering_checker.validate_battery_consistency(frozen({"bomLines": self.LINES}), frozen(self.PROFILE), {})
        assert_same({"status": "VALID", "findings": []}, result)

    def test_engineering_validation_with_a_non_numeric_profile_quantity(self):
        result = engineering_checker.validate_engineering(frozen({"bom": {"bomLines": self.LINES}, "profile": self.PROFILE, "sysType": "hybrid"}))
        expected = {
            "status": "WARNING",
            "findings": [
                {
                    "ruleId": "ENG-BAT-007",
                    "severity": "WARNING",
                    "description": "Battery protection mode not confirmed by Engineering",
                    "message": "Battery protection mode is not confirmed by Engineering. Existing template behaviour preserved (D12).",
                    "source": "decision D12 — per-battery confirmation pending",
                }
            ],
            "counts": {"blocked": 0, "warning": 1},
            "rulesEvaluated": 30,
        }
        assert_same(expected, result)


# ---------------------------------------------------------------------------------------------------------------------
# Rule-set documents: every problem is a RuleSetError, and a change that names no known rule is refused
# ---------------------------------------------------------------------------------------------------------------------


class TestRuleSetDocumentsRefuseEveryProblem:
    @pytest.mark.parametrize("code", [["PBC-A-001"], {"code": "PBC-A-001"}])
    def test_an_unhashable_rule_code_is_reported_not_raised(self, code):
        document = DEFAULT_RULE_SET.as_json()
        document["rules"][0]["code"] = code
        with pytest.raises(RuleSetError) as caught:
            RuleSet.from_json(document)
        assert any(error["path"] == "rules[0].code" and "unknown rule" in error["message"] for error in caught.value.errors), caught.value.errors

    @pytest.mark.parametrize("engine", [["engineeringChecker"], {"x": 1}])
    def test_an_unhashable_engine_is_reported_not_raised(self, engine):
        with pytest.raises(RuleSetError):
            RuleSet.from_json({**DEFAULT_RULE_SET.as_json(), "engine": engine})

    @pytest.mark.parametrize(
        ("changes", "path"),
        [
            ({"severities": {"PBC-X-999": "INFO"}}, "severities.PBC-X-999"),
            ({"params": {"PBC-X-999": {"minCellTemperatureC": 10}}}, "params.PBC-X-999"),
            ({"severities": {"PBC-D-003": "CRITICAL"}}, "severities.PBC-D-003"),
            ({"params": {"PBC-D-002": 5}}, "params.PBC-D-002"),
            ({"params": {"PBC-D-003": {"x": 1}}}, "rules[7].params.x"),
        ],
    )
    def test_with_changes_refuses_what_it_cannot_apply(self, changes, path):
        # a typo in a rule code used to produce a "new" version identical to the base, silently
        with pytest.raises(RuleSetError) as caught:
            DEFAULT_RULE_SET.with_changes(version="t.9", **changes)
        assert caught.value.code == "rule_set_invalid"
        assert any(error["path"] == path for error in caught.value.errors), caught.value.errors

    def test_with_changes_still_applies_valid_changes(self):
        rules = VALIDATION_RULE_SET.with_changes(version="v.9", severities={"ENG-CMP-005": "BLOCKED"})
        assert rules.rule("ENG-CMP-005").severity == engineering_checker.Severity.BLOCK and rules.version == "v.9"

    def test_a_non_finite_decimal_parameter_is_not_a_number(self):
        document = DEFAULT_RULE_SET.as_json()
        document["rules"][6]["params"]["minCellTemperatureC"] = Decimal("NaN")
        with pytest.raises(RuleSetError) as caught:
            RuleSet.from_json(document)
        assert any(error["path"] == "rules[6].params.minCellTemperatureC" for error in caught.value.errors)


# ---------------------------------------------------------------------------------------------------------------------
# Package checks: registry lines in the order packageApproval.runPackageChecker checks them
# ---------------------------------------------------------------------------------------------------------------------


class TestRegistryLinesOrder:
    #: ``[...roles].sort((a, b) => a.localeCompare(b))`` — the underscore sorts before letters (AC_CABLE < ACDB)
    LOCALE_ORDER = [
        "AC_CABLE",
        "AC_ISOLATOR",
        "ACDB",
        "BATTERY",
        "BATTERY_CABLE",
        "BATTERY_PROTECTION",
        "CHANGEOVER",
        "DC_CABLE",
        "DCDB",
        "EARTHING",
        "ENERGY_SYSTEM_CONTROLLER",
        "INVERTER",
        "METER",
        "MICRO_INVERTER",
        "MONITORING",
        "OTHER",
        "PANEL",
        "PROTECTION",
        "STRUCTURE",
    ]

    def test_lines_are_sorted_by_role_like_locale_compare(self):
        categories = {
            "STRUCTURE": "structure_material",
            "PROTECTION": "la_cable",
            "PANEL": "panel",
            "OTHER": "meter_box",
            "MONITORING": None,
            "MICRO_INVERTER": None,
            "METER": "meter",
            "INVERTER": "inverter",
            "ENERGY_SYSTEM_CONTROLLER": None,
            "EARTHING": "cb_rod",
            "DCDB": "dcdb",
            "DC_CABLE": "dc_cable",
            "CHANGEOVER": "change_over",
            "BATTERY_PROTECTION": "mccb_box",
            "BATTERY_CABLE": "battery_cable",
            "BATTERY": "battery",
            "ACDB": "acdb",
            "AC_ISOLATOR": "isolator",
            "AC_CABLE": "ac_cable",
        }
        enphase = {"MONITORING": "en3", "MICRO_INVERTER": "en1", "ENERGY_SYSTEM_CONTROLLER": "en7"}
        components = [{"role": category or "enphase", "componentId": enphase.get(role, f"c-{role.lower()}"), "quantity": 1} for role, category in categories.items()]
        assert [line["role"] for line in engineering_checker.registry_lines(components)] == self.LOCALE_ORDER

    def test_tied_findings_follow_the_legacy_line_order(self):
        # packageApproval.runPackageChecker on these components (captured): the four PBC-K-002 findings tie on
        # (ruleId, componentIds) and keep the localeCompare line order AC_CABLE, ACDB, DC_CABLE, DCDB
        components = [
            {"role": "panel", "componentId": "fx_pnl", "quantity": 6},
            {"role": "inverter", "componentId": "fx_inv_1p", "quantity": 1},
            {"role": "isolator", "componentId": "is1", "quantity": 1},
            {"role": "dcdb", "componentId": None, "quantity": 1},
            {"role": "acdb", "componentId": None, "quantity": 1},
            {"role": "ac_cable", "componentId": None, "quantity": 20},
            {"role": "dc_cable", "componentId": None, "quantity": 20},
            {"role": "earth_cable", "componentId": "nope-1", "quantity": 0},
        ]
        result = engineering_checker.check_project_bom(
            bom={"phase": "1P", "architecture": "DEYE", "sysType": "ongrid"},
            lines=engineering_checker.registry_lines(components),
            template_scope=True,
            catalog=FX_CATALOG,
            battery_master=FX_MASTER,
            at="2026-09-01T00:00:00.000Z",
            catalog_version="c1",
        )
        assert [(finding.rule_id, finding.message) for finding in result.findings] == [
            ("PBC-D-003", "String length is not declared in the project BOM."),
            ("PBC-K-002", "Role AC_CABLE is present but no component is selected."),
            ("PBC-K-002", "Role ACDB is present but no component is selected."),
            ("PBC-K-002", "Role DC_CABLE is present but no component is selected."),
            ("PBC-K-002", "Role DCDB is present but no component is selected."),
            ("PBC-K-003", 'Component "nope-1" (role EARTHING) is not in the catalog.'),
            ("PBC-N-001", "Inverter records mpptCount=2 and maxStringsPerMppt=1, but the BOM declares no string layout."),
        ]
        assert result.deterministic_key == "PBC-D-003|fx_inv_1p+fx_pnl;PBC-K-002|;PBC-K-002|;PBC-K-002|;PBC-K-002|;PBC-K-003|nope-1;PBC-N-001|fx_inv_1p"


# ---------------------------------------------------------------------------------------------------------------------
# Small parity gaps on the write paths
# ---------------------------------------------------------------------------------------------------------------------


class TestWritePathParity:
    def test_policy_not_found_prints_null_like_javascript(self):
        with pytest.raises(quotation_payload.PolicyError) as caught:
            quotation_payload.set_policy_status(frozen({"policies": []}), actor_id="u1", policy_id=None, status="INACTIVE", at="2026-09-20T10:00:00.000Z")
        assert caught.value.message == 'Policy "null" not found.'  # quotationPolicy.setPolicyStatus

    @pytest.mark.parametrize("fn", ["set_branding_account_status", "set_primary_branding_account"])
    def test_branding_account_not_found_prints_null_like_javascript(self, fn):
        store = frozen(load("rules_content.json")["refs"]["brandingStore"])
        extra = {"status": "ACTIVE"} if fn == "set_branding_account_status" else {}
        with pytest.raises(content_fit.BrandingError) as caught:
            getattr(content_fit, fn)(store, actor_id="u1", kind="bank", account_id=None, at="2026-09-20T10:00:00.000Z", **extra)
        assert caught.value.message == 'bank account "null" not found.'

    def test_a_pack_snapshot_without_a_material_list_stores_null(self):
        # createPackCommercialSnapshot({materialList: null}) keeps null (the [] default only replaces undefined)
        case = next(item for item in cases("rules_payload.json", "commercialSnapshot") if item["fn"] == "createPackCommercialSnapshot" and "error" not in item)
        data = case["input"]
        kwargs = {
            "snapshot_id": data["snapshotId"],
            "project_id": data["projectId"],
            "bom_snapshot_id": data["bomSnapshotId"],
            "pack_pricing": frozen(data["packPricing"]),
            "config_version": data["configVersion"],
            "cost_result": frozen(data.get("costResult")),
            "issued_by": data.get("issuedBy"),
            "issued_at": data.get("issuedAt"),
            "offer": frozen(data.get("offer")),
            "catalog_version": data.get("catalogVersion"),
        }
        snapshot = quotation_payload.create_pack_commercial_snapshot(material_list=None, **kwargs)
        assert snapshot["pack"]["materialList"] is None
        assert quotation_payload.create_pack_commercial_snapshot(**kwargs)["pack"]["materialList"] == ()
