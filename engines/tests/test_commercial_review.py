"""Adversarial review of engines-commercial: each finding reproduced by a failing test first; the tests stay.

R1 (division by zero → JS Infinity, never ZeroDivisionError) is pinned by golden cases recorded from the real
JavaScript (``structureQty/zero-size/*``, ``structureMaterial/zero-size``, ``price/edge/gst-minus-100``,
``bom/zero-watt/*``); the helper itself is unit-tested here.
"""

import copy
import math
import sys
import threading

import pytest

from engines import _jscompat as js
from engines import bom_builder, cost, pack_config, package_registry, pricing
from engines.tests.golden_harness import EXPECTED_DEVIATIONS, load_fixtures, load_golden


@pytest.fixture(scope="module")
def data():
    return load_fixtures()


@pytest.fixture()
def real(data):
    """Fresh copies per test: a test must never see another test's mutation."""
    return copy.deepcopy(data["catalog"]), copy.deepcopy(data["registry"]), copy.deepcopy(data["packStore"]["approved"]["config"])


# R1 ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [(1, 0, math.inf), (-1, 0, -math.inf), (1, -0.0, -math.inf), (-3, -0.0, math.inf), ("6", "3", 2), (7, 2, 3.5)],
)
def test_js_div(a, b, expected):
    assert js.js_div(a, b) == expected


@pytest.mark.parametrize(("a", "b"), [(0, 0), (math.nan, 0), (js.UNDEFINED, 0), (0, -0.0)])
def test_js_div_nan(a, b):
    assert math.isnan(js.js_div(a, b))


# R2 ------------------------------------------------------------------------------------------------------------


class TestValidateConfigIsASchema:
    def test_the_real_configs_are_valid(self, data):
        for copy_name in ("approved", "draft"):
            pack_config.validate_config(data["packStore"][copy_name]["config"])
        seeded = pack_config.seed_from_catalog(data["catalog"], at="2026-09-20T10:00:00.000Z")
        pack_config.schema.validate(seeded["approved"]["config"])

    def test_unknown_section_is_refused(self, data):
        config = {**data["packStore"]["approved"]["config"], "marketRate": {"ongrid_value": {"3": 229000}}}  # typo of marketRates
        with pytest.raises(pack_config.PackConfigError) as raised:
            pack_config.validate_config(config)
        assert raised.value.code == "INVALID_SECTION"
        assert raised.value.detail == {"section": "marketRate", "allowed": list(pack_config.CONFIG_SECTIONS)}

    @pytest.mark.parametrize("section", pack_config.CONFIG_SECTIONS)
    def test_every_section_is_required(self, data, section):
        config = {k: v for k, v in data["packStore"]["approved"]["config"].items() if k != section}
        with pytest.raises(pack_config.PackConfigError) as raised:
            pack_config.validate_config(config)
        assert raised.value.code == "INVALID_VALUE" and raised.value.detail == {"missing": [section]}

    def test_a_partial_config_is_refused(self):
        with pytest.raises(pack_config.PackConfigError) as raised:
            pack_config.validate_config({"gst": {"ratePct": 8.9}, "pricing": {"mode": "GROSS_MARGIN"}})
        assert raised.value.code == "INVALID_VALUE" and "bomTemplates" in raised.value.detail["missing"]


# R4 ------------------------------------------------------------------------------------------------------------


class TestBomResultsDoNotAliasTheInputs:
    def test_mutating_a_result_leaves_the_catalog_and_pack_config_alone(self, real):
        catalog, registry, config = real
        before = copy.deepcopy((catalog, registry, config))
        request = {"systemType": "ongrid", "size": "3", "tier": "value"}
        bom = bom_builder.build_bom(request, catalog=catalog, registry=registry, pack_config=config)
        panel = next(line for line in bom["lines"] if line["category"] == "panel")
        assert panel["alternatives"] == ["p2", "p7"]
        panel["alternatives"].append("p99")  # e.g. a service widening a copy of the result for display
        bom["profile"]["batteryIncluded"] = True
        assert (catalog, registry, config) == before
        again = bom_builder.build_bom(request, catalog=catalog, registry=registry, pack_config=config)
        assert next(line for line in again["lines"] if line["category"] == "panel")["alternatives"] == ["p2", "p7"]
        assert again["profile"]["batteryIncluded"] is False

    def test_refusal_detail_is_a_copy(self, real):
        catalog, registry, config = real
        with pytest.raises(bom_builder.BomBuildError) as raised:
            bom_builder.build_bom({"systemType": "ongrid", "size": "3", "tier": "value", "actorRole": "SALES", "selections": {"panel": "p4"}}, catalog=catalog, registry=registry, pack_config=config)
        assert raised.value.code == "SELECTION_NOT_APPROVED"
        raised.value.detail["approved"].append("p4")
        panel_slot = next(s for s in config["bomTemplates"]["ongrid"]["slots"] if s["category"] == "panel")
        assert panel_slot["alternatives"] == ["p2", "p7"]


# R5 ------------------------------------------------------------------------------------------------------------


def test_package_id_factory_never_repeats_an_id_across_threads():
    """Eight threads share one factory in the same millisecond: 1 200 ids (< 1 296 per ms) must all differ."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(10):
            factory = package_registry.PackageIdFactory(now_ms=0)
            ids = []

            def work(factory=factory, ids=ids):
                for _ in range(150):
                    ids.append(factory({"systemType": "ongrid", "size": "3", "phase": "1P", "tier": "value"}))

            threads = [threading.Thread(target=work) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert len(ids) == 1200 and len(set(ids)) == 1200
    finally:
        sys.setswitchinterval(previous)


# R6 ------------------------------------------------------------------------------------------------------------


def test_the_gross_margin_check_lives_in_engines_cost():
    """PLAN §2.3: LIST is never derived by markup in code; "gross-margin check lives in engines.cost"."""
    assert cost.gross_margin_list_price(160000, 0.2) == 200000
    with pytest.raises(cost.PricingError) as raised:
        cost.gross_margin_list_price(160000, 0.2, margin_type="MARKUP")
    assert raised.value.code == "MARGIN_TYPE_FORBIDDEN"
    assert cost.validate_gross_margin(1)["code"] == "MARGIN_OUT_OF_RANGE"
    assert cost.gross_margin_list_price is pricing.gross_margin_list_price


# R7 ------------------------------------------------------------------------------------------------------------


class TestSalesStripHidesEveryComponentPrice:
    def test_default_unit_price_is_stripped(self, real):
        catalog, registry, config = real
        lines = bom_builder.build_bom({"systemType": "ongrid", "size": "3", "tier": "value"}, catalog=catalog, registry=registry, pack_config=config)["lines"]
        stripped = bom_builder.strip_cost_fields_for_sales(lines)
        leaked = {key for line in stripped for key in line if key in {"unitPrice", "amount", "gst", "gstAmt", "defaultUnitPrice", "landedUnitCost", "purchasePrice", "supplier"}}
        assert leaked == set()
        # p2 is its own default: the JS left defaultUnitPrice = 13585 = the unitPrice it had just removed.
        assert next(line for line in lines if line["componentId"] == "p2")["defaultUnitPrice"] == 13585

    def test_the_golden_deviation_is_real(self):
        """The recorded JavaScript output still carries defaultUnitPrice; the harness removes it (DV-25) before comparing."""
        [case] = [c for c in load_golden("bom")["cases"] if c["fn"] == "stripCostFieldsForSales"]
        assert any("defaultUnitPrice" in line for line in case["result"])
        assert not any("defaultUnitPrice" in line for line in EXPECTED_DEVIATIONS["stripCostFieldsForSales"](case["result"]))


# R8 ------------------------------------------------------------------------------------------------------------


PLATFORM_ROLES = ["sales-executive", "Sales Executive", "project-head", "sales", "CEO"]


class TestUnknownRolesAreHeldToTheSalesRules:
    @pytest.mark.parametrize("role", PLATFORM_ROLES)
    def test_build_bom_refuses_an_unapproved_swap(self, real, role):
        catalog, registry, config = real
        with pytest.raises(bom_builder.BomBuildError) as raised:
            bom_builder.build_bom({"systemType": "ongrid", "size": "3", "tier": "value", "actorRole": role, "selections": {"panel": "p4"}}, catalog=catalog, registry=registry, pack_config=config)
        assert raised.value.code == "SELECTION_NOT_APPROVED"

    @pytest.mark.parametrize("role", PLATFORM_ROLES)
    def test_build_bom_refuses_a_locked_slot(self, real, role):
        catalog, registry, config = real
        with pytest.raises(bom_builder.BomBuildError) as raised:
            bom_builder.build_bom({"systemType": "ongrid", "size": "3", "tier": "value", "actorRole": role, "selections": {"dcdb": "d3"}}, catalog=catalog, registry=registry, pack_config=config)
        assert raised.value.code == "SELECTION_NOT_PERMITTED"

    @pytest.mark.parametrize("role", PLATFORM_ROLES)
    def test_get_alternatives_shows_no_prices_and_only_swappable_slots(self, real, role):
        catalog, registry, config = real
        view = bom_builder.get_alternatives({"systemType": "ongrid", "size": "3", "tier": "value", "actorRole": role}, catalog=catalog, registry=registry, pack_config=config)
        assert all(slot["salesSwap"] for slot in view["slots"])
        assert not any("price" in item or "panelType" in item for items in view["alternatives"].values() for item in items)

    @pytest.mark.parametrize("role", ["PROJECT_HEAD", "ADMIN"])
    def test_flarize_roles_keep_their_view(self, real, role):
        catalog, registry, config = real
        bom = bom_builder.build_bom({"systemType": "ongrid", "size": "3", "tier": "value", "actorRole": role, "selections": {"dcdb": "d3"}}, catalog=catalog, registry=registry, pack_config=config)
        assert next(line for line in bom["lines"] if line["category"] == "dcdb")["componentId"] == "d3"
        view = bom_builder.get_alternatives({"systemType": "ongrid", "size": "3", "tier": "value", "actorRole": role}, catalog=catalog, registry=registry, pack_config=config)
        assert any("price" in item for items in view["alternatives"].values() for item in items)
