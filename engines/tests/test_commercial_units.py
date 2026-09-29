"""Unit tests for what the golden files cannot reach: the JS-semantics helpers, the Python-only additions
(``allocate_landed`` charge rules, ``gross_margin_list_price``, ``validate_config``, the injectable package-id factory and
checker, the authorisation hook) and the purity of the ``engines`` package."""

import ast
import copy
import math
import re
from decimal import Decimal
from pathlib import Path

import pytest

from engines import _jscompat as js
from engines import cost, flarize_rbac, money, offers, pack_config, package_registry, pricing
from engines.money import is_money_binary64 as is_money
from engines.money import mul_exact_binary64 as mul_exact
from engines.money import round_money_binary64 as round_money
from engines.money import sum_exact_binary64 as sum_exact


class TestJsNumbers:
    @pytest.mark.parametrize(
        ("value", "text"),
        [
            (0, "0"),
            (-0.0, "0"),
            (1.0, "1"),
            (123116.3, "123116.3"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1e21, "1e+21"),
            (1.5e-7, "1.5e-7"),
            (0.000001, "0.000001"),
            (123456789012345680000.0, "123456789012345680000"),
            (2**60, "1152921504606847000"),
            (math.nan, "NaN"),
            (-math.inf, "-Infinity"),
            (Decimal("12.50"), "12.5"),
        ],
    )
    def test_number_to_string(self, value, text):
        assert js.js_str(value) == text

    def test_other_values_to_string(self):
        assert [js.js_str(v) for v in (None, js.UNDEFINED, True, "x", [1, None, [2, 3]], {"a": 1})] == ["null", "undefined", "true", "x", "1,,2,3", "[object Object]"]

    @pytest.mark.parametrize(
        ("value", "number"),
        [(" 12 ", 12), ("", 0), ("0x1F", 31), ("1e3", 1000.0), ("-Infinity", -math.inf), ([5], 5), ([], 0), (None, 0), (True, 1), (2**60, float(2**60))],
    )
    def test_number_coercion(self, value, number):
        assert js.js_number(value) == number

    @pytest.mark.parametrize("value", [js.UNDEFINED, "abc", "1_000", "inf", {}, [1, 2], "12px"])
    def test_number_coercion_nan(self, value):
        assert math.isnan(js.js_number(value))

    @pytest.mark.parametrize(("value", "rounded"), [(2.5, 3), (-2.5, -2), (0.49999999999999994, 0), (-0.4, 0), (1e300, 1e300)])
    def test_math_round(self, value, rounded):
        assert js.js_round(value) == rounded

    def test_math_round_nan_passthrough(self):
        assert math.isnan(js.js_round(math.nan)) and js.js_floor(math.inf) == math.inf and math.isnan(js.js_ceil("x"))

    @pytest.mark.parametrize(("value", "digits", "text"), [(1.005, 2, "1.00"), (2.5, 0, "3"), (-1.25, 1, "-1.3"), (-0.04, 1, "-0.0"), (1e21, 1, "1e+21"), (math.nan, 1, "NaN")])
    def test_to_fixed(self, value, digits, text):
        assert js.js_to_fixed(value, digits) == text

    def test_parse_float_and_min_max(self):
        assert js.parse_float("5sp") == 5 and math.isnan(js.parse_float("sp5")) and js.parse_float("-Infinity kW") == -math.inf
        assert math.isnan(js.js_min(1, math.nan)) and js.js_min() == math.inf and js.js_max() == -math.inf and math.isnan(js.js_max("x", 1))
        assert js.to_base36(0) == "0" and js.to_base36(-71) == "-1z"

    def test_add_concatenates_strings(self):
        assert js.js_add("4", 3) == "43" and js.js_add(4, 3) == 7 and js.js_add([1], {}) == "1[object Object]"

    @pytest.mark.parametrize(("value", "prefix"), [("൩", math.nan), ("٣", math.nan), ("３", math.nan), ("1٣", 1), ("0x１", 0)])
    def test_digits_are_ascii(self, value, prefix):
        """``Number()`` and ``parseFloat()`` read ASCII digits only (the value rules shared with ``engines.jscompat``):
        a quantity typed in Malayalam, Arabic-Indic or full-width digits is NaN, as in JavaScript (Python's ``float``
        would read it as a number); ``parseFloat`` keeps the ASCII prefix."""
        assert math.isnan(js.js_number(value))
        parsed = js.parse_float(value)
        assert math.isnan(parsed) if math.isnan(prefix) else parsed == prefix


class TestJsObjects:
    def test_keys_order(self):
        assert js.js_keys({"b": 1, "10": 2, "2": 3, "01": 4}) == ["2", "10", "b", "01"]
        assert js.js_keys(["x", "y"]) == ["0", "1"] and js.js_keys("ab") == ["0", "1"] and js.js_keys(5) == []
        assert js.js_entries("ab") == [("0", "a"), ("1", "b")] and js.js_entries(None) == []

    def test_array_indexes_are_canonical_ascii_integers(self):
        assert js.js_keys({"b": 1, "1٣": 2, "1": 3}) == ["1", "b", "1٣"]
        assert js.jsget(["a", "b"], "1\n") is js.UNDEFINED and js.jsget(["a", "b"], "1") == "b"

    def test_property_access(self):
        assert js.jsget({"3": "x"}, 3) == "x"
        assert js.jsget(["a"], "0") == "a" and js.jsget(["a"], "length") == 1 and js.jsget(["a"], 5) is js.UNDEFINED
        assert js.jsget("abc", 1) == "b" and js.jsget("abc", "length") == 3 and js.jsget(None, "a") is js.UNDEFINED
        assert js.jsget_path({"a": {"b": 1}}, "a", "b") == 1 and js.jsget_path({"a": None}, "a", "b") is js.UNDEFINED

    def test_truthiness_and_operators(self):
        assert [js.truthy(v) for v in ([], {}, 0, math.nan, "", "0", js.UNDEFINED)] == [True, True, False, False, False, True, False]
        assert js.js_or(0, "", "x") == "x" and js.js_or(0, None) is None
        assert js.nullish(None, js.UNDEFINED, 0) == 0 and js.nullish(None, js.UNDEFINED) is js.UNDEFINED

    def test_strict_equality_and_includes(self):
        assert js.strict_equal(1, 1.0) and not js.strict_equal(1, True) and not js.strict_equal("1", 1)
        assert not js.strict_equal(None, js.UNDEFINED) and not js.strict_equal({}, {}) and js.strict_equal(True, True)
        assert js.includes([math.nan], math.nan) and js.includes("1P-HYB", "HYB") and not js.includes(5, 5)

    def test_stringify_matches_json_stringify(self):
        value = {"b": [1.0, js.UNDEFINED, math.nan], "10": "é\n", "2": True, "skip": js.UNDEFINED, "n": None, "d": Decimal("2.50")}
        assert js.js_stringify(value) == '{"2":true,"10":"é\\n","b":[1,null,null],"n":null,"d":2.5}'
        assert js.js_stringify(js.UNDEFINED) is js.UNDEFINED and js.js_stringify(object()) is js.UNDEFINED
        assert js.json_equal({"a": 1}, {"a": 1.0}) and not js.json_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})

    def test_locale_compare(self):
        assert js.locale_compare("AC_ISOLATOR", "ACDB") < 0  # punctuation sorts before letters (ICU), unlike code points
        assert js.locale_compare("a", "B") < 0 and js.locale_compare("b", "A") > 0
        assert js.locale_compare("a", "A") < 0 and js.locale_compare("é", "e") > 0 and js.locale_compare("x", "x") == 0
        assert js.locale_compare("2026-09-10T00:00:00.000Z", "2026-09-09T23:00:00.000Z") > 0
        assert js.locale_compare("2", "10") > 0 and js.locale_compare("€", "$") > 0

    def test_utf16_order(self):
        assert js.js_str_lt("\uffff", "\U0001f600") is False and js.js_str_lt("a", "b")
        assert sorted(["\U0001f600", "\uffff"], key=js.js_str_key) == ["\U0001f600", "\uffff"]

    def test_json_numbers_and_trim(self):
        assert js.json_numbers({"a": [Decimal("1.50"), Decimal("2")], "b": (Decimal("0.1"),)}) == {"a": [1.5, 2], "b": [0.1]}
        assert js.js_trim("\ufeff x \u3000") == "x"

    def test_undefined_is_a_singleton(self):
        assert copy.deepcopy(js.UNDEFINED) is js.UNDEFINED and repr(js.UNDEFINED) == "undefined" and not js.UNDEFINED

    def test_js_error_as_dict(self):
        error = js.JsError("boom", "CODE", {"a": 1})
        assert error.as_dict() == {"name": "Error", "message": "boom", "code": "CODE", "detail": {"a": 1}}


class TestMoney:
    def test_rule(self):
        assert [round_money(v) for v in (1.5, -1.5, 2.5, -0.4, "12.5", None, "x", math.inf)] == [2, -2, 3, 0, 13, None, None, None]
        assert sum_exact([1, "2", None, "x"]) == 3 and sum_exact(None) == 0 and mul_exact("3", None) == 0
        assert [is_money(v) for v in ("", " ", "1", None, math.nan, [])] == [False, True, True, False, False, True]

    def test_one_money_module_two_number_types(self):
        """The binary64 rule lives in ``engines.money`` beside the Decimal one (consolidated at the wave-1 integration):
        both give the same rupee for amounts a double holds exactly, only the binary64 one takes floats, and only it needs
        the ``Number.EPSILON`` nudge (1.005 × 100 is 100.49999999999999 as a double; roundMoney publishes 101)."""
        for value in (0.5, 1.5, -2.5, 12.25, -0.75, 1234567.5):
            assert round_money(value) == int(money.round_money(Decimal(repr(value))))
        assert round_money(1.005 * 100) == 101 and money.round_money(Decimal(repr(1.005 * 100))) == 100
        with pytest.raises(TypeError):
            money.round_money(1.5)


class TestOneBatteryPort:
    """``battery_compat``'s master/compatibility/protection functions run engines-rules' port (consolidated at the wave-1
    integration); the commercial goldens replay through it, and the commercial calling convention is kept."""

    ITEM = {"id": "b", "brand": "Deye", "name": "B", "tiers": ["base"]}
    MASTER = {"compatibleSystemTypes": ["hybrid"], "integratedProtection": True, "capacityKwh": 5.12, "maximumDischargeCurrent": 100}
    CONTEXT = {"inverter": {"id": "i", "batteryVoltageMin": 40, "batteryVoltageMax": 60}, "quantity": 2.5}

    def test_runs_the_rules_port_with_javascript_numbers(self, monkeypatch):
        from engines import battery_compat, engineering_checker

        calls = []
        real = engineering_checker.check_battery_compatibility
        monkeypatch.setattr(engineering_checker, "check_battery_compatibility", lambda *args: calls.append(args) or real(*args))
        master = battery_compat.to_battery_master(self.ITEM, {**self.MASTER, "nominalVoltage": 51.2})
        result = battery_compat.check_battery_compatibility(master, self.CONTEXT)
        assert calls and type(master) is dict and master["capacityKwh"] == 5.12 and isinstance(master["capacityKwh"], float)
        assert type(result) is dict and all(type(check) is dict for check in result["checks"])
        assert {check["id"]: check["result"] for check in result["checks"]}["BC-C"] == "PASS"
        assert [check["message"] for check in result["checks"] if check["id"] == "BC-I"] == ["Quantity 2.5."]

    def test_unicode_digits_are_not_numbers(self):
        """``Number('൫൧')`` is NaN: a voltage typed in Malayalam digits fails BC-C (the commercial copy read it as 51 V)."""
        from engines import battery_compat

        master = battery_compat.to_battery_master(self.ITEM, {**self.MASTER, "nominalVoltage": "൫൧"})
        checks = {check["id"]: check for check in battery_compat.check_battery_compatibility(master, self.CONTEXT)["checks"]}
        assert checks["BC-C"]["result"] == "FAIL" and checks["BC-C"]["message"] == "Battery ൫൧ V is outside the inverter window 40–60 V."

    def test_a_record_that_is_not_a_master_record_is_a_type_error(self):
        from engines import battery_compat

        with pytest.raises(TypeError):
            battery_compat.check_battery_compatibility({"brand": "Deye"}, {})


class TestLandedAllocation:
    LINES = [{"componentId": "a", "quantity": 10, "purchaseUnitPrice": 100}, {"componentId": "b", "quantity": 5, "purchaseUnitPrice": 200}]

    def test_multiple_charges_are_allocated_as_one_sum(self):
        charges = [{"kind": "FREIGHT", "amount": 300}, {"kind": "INSURANCE", "amount": 100.5}, {"kind": "DUTY", "amount": Decimal("99.5")}]
        together = cost.allocate_landed(self.LINES, [{"kind": k["kind"], "amount": float(k["amount"])} for k in charges], batch_id="B-1")
        assert together["ok"] and together["allocation"]["deliveryCost"] == 500
        assert [line["allocatedDeliveryCost"] for line in together["lines"]] == [250, 250]
        assert [line["landedUnitCost"] for line in together["lines"]] == [125, 250]

    @pytest.mark.parametrize("charge", [{"kind": "TRANSPORT_TO_SITE", "amount": 5}, {"kind": "FREIGHT", "amount": -1}, {"kind": "FREIGHT"}, {"kind": "FREIGHT", "amount": "x"}])
    def test_invalid_charges_refuse_the_batch(self, charge):
        result = cost.allocate_landed(self.LINES, [charge], batch_id="B-2")
        assert result["ok"] is False and result["errors"][0]["code"] == "BATCH_CHARGE_INVALID" and result["lines"] == []

    def test_forbidden_method_passes_through(self):
        result = cost.allocate_landed(self.LINES, [], allocation_method="WEIGHT")
        assert result["errors"][0]["code"] == "ALLOCATION_METHOD_INVALID"

    def test_price_master_entries(self):
        result = cost.allocate_landed(self.LINES, [{"kind": "FREIGHT", "amount": 30}])
        entries = cost.to_price_master_entries(result, landed_cost_version="B::lc")
        assert entries["a"]["landedUnitCost"] == 102 and entries["a"]["landedCostBuildUp"]["allocationMethod"] == "PURCHASE_VALUE_PROPORTION"


class TestPricingPython:
    @pytest.mark.parametrize(("margin", "code"), [(1, "MARGIN_OUT_OF_RANGE"), (-0.1, "MARGIN_OUT_OF_RANGE"), (None, "MARGIN_NOT_CONFIGURED")])
    def test_list_price_refuses_bad_margins(self, margin, code):
        with pytest.raises(pricing.PricingError) as raised:
            pricing.gross_margin_list_price(100, margin)
        assert raised.value.code == code

    def test_list_price_refuses_unknown_type(self):
        with pytest.raises(pricing.PricingError) as raised:
            pricing.gross_margin_list_price(100, 0.2, margin_type="NET")
        assert raised.value.code == "MARGIN_TYPE_FORBIDDEN"
        assert pricing.gross_margin_list_price(Decimal("80"), Decimal("0.2")) == 100


class TestPackConfigPython:
    def test_validate_config(self):
        config = pack_config.seed_from_catalog({}, at="2026-09-20")["approved"]["config"]
        pack_config.validate_config(config)
        with pytest.raises(pack_config.PackConfigError) as raised:
            pack_config.validate_config({**config, "extra": 1})  # unknown sections are refused (review R2)
        assert raised.value.code == "INVALID_SECTION"
        with pytest.raises(pack_config.PackConfigError) as raised:
            pack_config.validate_config({**config, "gst": {"ratePct": 200}})
        assert raised.value.code == "INVALID_VALUE"
        with pytest.raises(pack_config.PackConfigError):
            pack_config.validate_config([])
        assert pack_config.schema.NAME == "flarize.pack-config/1" and pack_config.schema.validate is pack_config.validate_config
        with pytest.raises(pack_config.PackConfigError) as raised:
            pack_config.schema.validate_section("colours", {})
        assert raised.value.code == "INVALID_SECTION"

    def test_accessors_and_authorize_hook(self):
        store = pack_config.seed_from_catalog({"marketRates": {"ongrid_value": {"3": 1}}}, at="2026-09-20")
        assert pack_config.approved_config(store) == pack_config.draft_config(store)
        assert pack_config.approved_config(None) is None and pack_config.draft_config({}) is None
        platform_actor = {"userId": "u-uid", "role": "project-head"}
        with pytest.raises(flarize_rbac.RbacError) as raised:
            pack_config.update_draft_section(store, actor=platform_actor, section="gst", value={"ratePct": 9}, at="2026-09-21")
        assert raised.value.code == "UNKNOWN_ROLE"
        edited = pack_config.update_draft_section(store, actor=platform_actor, section="gst", value={"ratePct": 9}, at="2026-09-21", authorize=flarize_rbac.allow_all)
        submitted = pack_config.submit_draft(edited, actor=platform_actor, at="2026-09-21", authorize=flarize_rbac.allow_all)
        approved = pack_config.approve_draft(submitted, actor={"userId": "admin"}, at="2026-09-22", authorize=flarize_rbac.allow_all)
        assert approved["approved"]["version"] == 2 and approved["approved"]["config"]["gst"] == {"ratePct": 9} and store["approved"]["version"] == 1

    def test_rbac_matrix(self):
        assert flarize_rbac.can("ADMIN", "PACKAGE_APPROVE") and not flarize_rbac.can("SALES", "PACKAGE_EDIT") and not flarize_rbac.can("X", "PACKAGE_EDIT")
        assert flarize_rbac.actor_label({"role": "ADMIN"}) == "ADMIN" and flarize_rbac.actor_label({"userId": "u", "role": "ADMIN"}) == "u"


class TestPackageRegistryPython:
    def _registry(self):
        return {
            "packages": [
                {
                    "packageId": "A",
                    "systemType": "ongrid",
                    "size": "3",
                    "phase": "1P",
                    "tier": "value",
                    "profileKey": "ongrid_premium",
                    "packageState": "DRAFT",
                    "components": [{"role": "panel", "componentId": "p1", "quantity": 6}, {"role": "enphase", "componentId": "en1"}, {"role": "isolator", "componentId": "is1"}, {"role": "mystery"}],
                },
                {"packageId": "B", "systemType": "ongrid", "size": "3", "phase": "1P", "tier": "value", "architecture": "LG", "components": []},
                {"packageId": "C", "profileKey": "none", "components": []},
            ]
        }

    def test_run_package_checker_uses_injected_checker(self):
        registry = self._registry()
        seen = {}

        def checker(payload):
            seen.update(payload)
            return {"status": "VALID", "findings": []}

        env = {"catalog": {"packageProfiles": {"ongrid_premium": {"inverterType": "micro"}}}}
        validation = package_registry.run_package_checker(registry, env, actor={"userId": "e", "role": "ENGINEERING"}, package_id="A", at="t", check_project_bom=checker)
        assert validation == {"status": "VALID", "findings": [], "revisionNumber": 1, "architecture": "ENPHASE", "architectureSource": "PROFILE_INVERTER_TYPE"}
        assert [line["role"] for line in seen["lines"]] == ["AC_ISOLATOR", "MICRO_INVERTER", "OTHER", "PANEL"]
        assert seen["templateScope"] is True and seen["bom"]["architecture"] == "ENPHASE" and seen["batteryMaster"] == {}

    @pytest.mark.parametrize(("package_id", "code"), [("B", "ARCHITECTURE_INVALID"), ("C", "ARCHITECTURE_UNDECLARED"), ("Z", "PACKAGE_NOT_FOUND")])
    def test_run_package_checker_refusals(self, package_id, code):
        with pytest.raises(package_registry.ApprovalError) as raised:
            package_registry.run_package_checker(self._registry(), None, actor={"userId": "e", "role": "ENGINEERING"}, package_id=package_id, at="t", check_project_bom=lambda p: {})
        assert raised.value.code == code

    def test_id_factory(self):
        factory = package_registry.PackageIdFactory(now_ms=0, seq=1295)
        assert factory({"systemType": "hybrid", "size": "8", "phase": "3p", "tier": "value", "variant": "FUTURE_READY", "systemSize": "10"}) == "CFGPKG-HYBRID-8-3P-VALUE-FR10-0zz"
        assert factory({}) == "CFGPKG-PKG----000"
        live = package_registry.PackageIdFactory()({"systemType": "ongrid"})
        assert re.fullmatch(r"CFGPKG-ONGRID----[0-9a-z]+00", live)


class TestOffersPython:
    def test_clock_defaults(self):
        offer = offers.create_draft_offer({"name": "N"}, "admin")
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", offer["createdAt"]) and offer["id"].startswith("offer_")
        offers.transition_offer(offer, "APPROVED", "admin")
        assert offer["status"] == "APPROVED" and len(offer["transitions"]) == 2
        active = {"status": "ACTIVE", "endDate": "2000-01-01", "startDate": ""}
        assert offers.auto_expire_offers([active]) == [active] and active["status"] == "EXPIRED"
        assert offers.find_applicable_offer([{"status": "ACTIVE"}]) == {"status": "ACTIVE"}


def test_engines_import_nothing_from_django_or_apps():
    """The import-linter contract, checked here too so a violation fails fast in the engine suite."""
    forbidden = re.compile(r"^(django|rest_framework|celery|flarize|core|accounts|catalog|pricing|packs|quotations)(\.|$)")
    for path in Path(__file__).resolve().parents[1].glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not [n for n in names if forbidden.match(n)], f"{path.name} imports {names}"
