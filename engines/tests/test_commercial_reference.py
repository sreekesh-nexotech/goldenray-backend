"""The reference outputs of flarize-engines-spec.md §20 (and the worked examples of §9, §13, §14), asserted literally.

These are independent of the golden files: they pin the business numbers the owner signed off, computed by the Python
engines from the committed Flarize data fixtures.
"""

import copy

import pytest

from engines import battery_compat, bom_builder, cost, offers, pack_pricing, pricing
from engines.tests.golden_harness import load_fixtures


@pytest.fixture(scope="module")
def data():
    return load_fixtures()


@pytest.fixture(scope="module")
def approved(data):
    return data["packStore"]["approved"]["config"]


@pytest.fixture(scope="module")
def value3(data, approved):
    return bom_builder.build_bom({"systemType": "ongrid", "size": "3", "tier": "value", "phase": "1P"}, catalog=data["catalog"], registry=data["registry"], pack_config=approved)


def _price(approved, lines, roof, distance=130):
    return pack_pricing.price_pack(config=approved, system_type="ongrid", size="3", tier="value", roof_type=roof, distance_km=distance, lines=lines)


class TestBomReference:
    def test_ongrid_3_value_lines(self, value3):
        variable = {line["category"]: line for line in value3["lines"] if line["isVariable"]}
        assert (variable["panel"]["componentId"], variable["panel"]["qty"], variable["panel"]["unitPrice"], variable["panel"]["gst"]) == ("p2", 6, 13585, 5)
        assert (variable["inverter"]["componentId"], variable["inverter"]["unitPrice"]) == ("i20", 18250)
        expected = {"dcdb": ("d2", 1), "acdb": ("a1", 1), "meter": ("m2", 1), "dc_cable": ("dc3", 50), "ac_cable": ("ac2", 20), "isolator": ("is1", 1)}
        expected.update({"cb_rod": ("cb4", 3), "earth_cable": ("ec2", 20), "la_cable": ("la1", 20)})
        for category, (component, qty) in expected.items():
            assert (variable[category]["componentId"], variable[category]["qty"]) == (component, qty)
        assert all(line["selectionMethod"] == "PACK_CONFIG_DEFAULT" for line in variable.values())
        assert len([line for line in value3["lines"] if line["category"] == "fixed"]) == 35

    def test_ongrid_3_value_totals(self, value3):
        assert value3["totals"] == {"matTotal": 123116.3, "allGst": 9195, "sub": 123116.3, "grand": 132311.3}
        assert value3["engineering"] == {"blockers": [], "status": "VALID"}

    def test_premium_hybrid_is_enphase(self, data, approved):
        bom = bom_builder.build_bom({"systemType": "hybrid", "size": "10", "tier": "premium"}, catalog=data["catalog"], registry=data["registry"], pack_config=approved)
        by_id = {line["componentId"]: line for line in bom["lines"]}
        assert by_id["en1"]["qty"] == by_id["p3"]["qty"] and by_id["en1"]["selectionMethod"] == "DEVICE_ALLOCATION_STRUCTURED"
        assert (by_id["en7"]["unitPrice"], by_id["en7"]["gst"]) == (110000, 18)
        assert (by_id["ug_4core"]["qty"], by_id["ug_4core"]["gst"]) == (40, 18)
        assert by_id["bt2"]["category"] == "battery"
        assert "inverter" not in {line["category"] for line in bom["lines"]}

    def test_unsupported_three_phase_3kw(self, data, approved):
        with pytest.raises(bom_builder.BomBuildError) as raised:
            bom_builder.build_bom({"systemType": "ongrid", "size": "3", "tier": "value", "phase": "3P"}, catalog=data["catalog"], pack_config=approved)
        assert raised.value.code == "UNSUPPORTED_CONFIGURATION"


class TestPackPricingReference:
    def test_flat_130km(self, approved, value3):
        priced = _price(approved, value3["lines"], "FLAT")
        customer = priced["customer"]
        assert (customer["marketRate"], customer["roofAddOn"], customer["sellingPriceIncludingGST"]) == (229000, 0, 229000)
        assert (customer["sellingPriceBeforeGST"], customer["gstAmount"]) == (210285, 18715)
        assert (customer["transportExtra"], customer["transportExtraDetail"]["extraKm"], customer["transportExtraDetail"]["ratePerKm"]) == (1050, 30, 35)
        assert customer["customerTotalIncludingGST"] == 230050
        internal = priced["internal"]
        assert internal["referenceCost"] == {
            "material": 123116,
            "structureMaterial": 9972,
            "installation": 15000,
            "service": 10000,
            "transportationBase": 4000,
            "miscellaneous": 1000,
            "office": 5500,
            "structureLabour": 0,
            "structureRepair": 0,
        }
        assert (internal["referenceTotal"], internal["grand"], internal["marginVsMarket"], internal["marginVsMarketPct"]) == (168588, 220311, 8689, 3.9)

    def test_sheet_roof_add_on(self, approved, value3):
        priced = _price(approved, value3["lines"], "SHEET")
        detail = priced["roofAddOnDetail"]
        assert (detail["installationDiff"], detail["structureRoofTotal"], detail["structureFlatTotal"], detail["structureExtra"]) == (3000, 16556, 9972, 6584)
        assert (detail["structureLabour"], detail["structureRepair"]) == (3000, 958)
        customer = priced["customer"]
        assert (customer["roofAddOnPreGst"], customer["roofAddOnGst"], customer["roofAddOn"]) == (13542, 1205, 14747)
        assert (customer["sellingPriceIncludingGST"], customer["transportExtra"], customer["customerTotalIncludingGST"]) == (243747, 1050, 244797)

    def test_swap_delta_p2_to_p7(self, data, approved):
        bom = bom_builder.build_bom(
            {"systemType": "ongrid", "size": "3", "tier": "value", "selections": {"panel": "p7"}, "actorRole": "SALES"},
            catalog=data["catalog"],
            registry=data["registry"],
            pack_config=approved,
        )
        priced = _price(approved, bom["lines"], "FLAT")
        [delta] = priced["swapDeltas"]
        assert (delta["selected"], delta["default"], delta["qty"]) == ("p7", "p2", 6)
        assert (delta["diffPreGst"], delta["diffGst"], delta["diff"], delta["gstPct"]) == (-1722, -86, -1808, 5)
        assert priced["customer"]["sellingPriceIncludingGST"] == 227192

    def test_hybrid_5_blocks_without_installation_row(self, data, approved):
        bom = bom_builder.build_bom({"systemType": "hybrid", "size": "5", "tier": "value"}, catalog=data["catalog"], registry=data["registry"], pack_config=approved)
        priced = pack_pricing.price_pack(config=approved, system_type="hybrid", size="5", tier="value", roof_type="FLAT", distance_km=10, lines=bom["lines"], battery_config=1)
        assert priced["status"] == "BLOCKED"
        assert {e["code"] for e in priced["errors"]} == {"MARKET_RATE_NOT_SET", "INSTALLATION_NOT_SET"}


class TestCostAndPricingReference:
    def test_gst_composite_is_8_9(self, data):
        resolved = pricing.resolve_gst_regime(data["costConfig"]["gst"])
        assert resolved["effectiveRatePct"] == 8.9
        assert [(c["label"], c["valuationPct"], c["ratePct"]) for c in resolved["components"]] == [("GOODS", 70, 5), ("SERVICE", 30, 18)]

    def test_cost_heads(self, data):
        snapshot = {"projectId": "P1", "status": "LOCKED", "lines": [{"componentId": "p2", "quantity": 6}, {"componentId": "i20", "quantity": 1}]}
        project = {"sizeKw": 3, "installationType": "FLAT", "distanceKm": 130, "vehicleType": "STANDARD", "structureMaterialCost": 0, "tier": "value"}
        result = cost.calculate_cost(snapshot=snapshot, config=data["costConfig"], price_master=data["priceMaster"], project=project)
        assert result["status"] == "COMPLETE"
        assert (result["siteSurveyCost"], result["serviceAmcCost"], result["officeExpenseAllocation"], result["installationCost"]) == (590, 10000, 15000, 15000)
        assert (result["structureCost"], result["engineeringDesignCost"], result["transportationCost"]) == (3000, 500, 130 * 35)
        assert result["miscellaneousCost"] == round(result["directProjectCostExact"] / 100)

    def test_gross_margin_never_markup(self, data):
        cost_result = {"status": "COMPLETE", "totalActualProjectCostExact": 160000, "tier": "VALUE"}
        priced = pricing.calculate_pricing(cost_result=cost_result, margin=data["costConfig"]["margin"], gst=data["costConfig"]["gst"])
        assert priced["listSellingPriceBeforeGST"] == 200000  # 160000 / (1 − 0.20), not 160000 × 1.20
        assert priced["gstAmount"] == 7000 + 10800 and priced["priceIncludingGST"] == 217800
        markup = pricing.calculate_pricing(cost_result=cost_result, margin={"marginType": "MARKUP", "targetGrossMargin": 0.2}, gst=data["costConfig"]["gst"])
        assert (markup["status"], markup["error"]) == ("REJECTED", "MARGIN_TYPE_FORBIDDEN")
        with pytest.raises(pricing.PricingError) as raised:
            pricing.gross_margin_list_price(160000, 0.2, margin_type="MARKUP")
        assert raised.value.code == "MARGIN_TYPE_FORBIDDEN"
        assert pricing.gross_margin_list_price(160000, 0.2) == 200000

    def test_landed_allocation_example(self):
        lines = [{"componentId": c, "quantity": q, "purchaseUnitPrice": 1000} for c, q in (("a", 70), ("b", 20), ("c", 10))]
        result = cost.allocate_landed(lines, [{"kind": "FREIGHT", "amount": 5000}])
        assert [line["allocatedDeliveryCost"] for line in result["lines"]] == [3500, 1000, 500]
        assert result["allocation"]["reconciled"] is True


class TestOffersAndBatteryReference:
    def test_catalog_offer_applies(self, data):
        offer = offers.find_applicable_offer(copy.deepcopy(data["catalog"]["offers"]), system_type="ongrid", tier="value", size="3", date="2026-09-20")
        assert (offer["name"], offer["type"], offer["value"]) == ("Monsoon 2026 Discount", "flat", 5000)
        assert offers.calculate_offer_amount(offer, 210285) == 5000

    def test_bt1_blocked_bt2_integrated(self, data):
        items = {i["id"]: i for i in data["catalog"]["categories"]["battery"]["items"]}
        bt1 = battery_compat.to_battery_master(items["bt1"], data["batteryMaster"]["bt1"])
        bt2 = battery_compat.to_battery_master(items["bt2"], data["batteryMaster"]["bt2"])
        check = battery_compat.check_battery_compatibility(bt1, {"sysType": "hybrid", "quantity": 1})
        assert check["status"] == "BLOCKED"
        assert next(c for c in check["checks"] if c["id"] == "BC-G")["detail"] == {"missing": "protectionRating", "safetyCritical": True}
        assert battery_compat.resolve_protection_requirement(bt2)["mode"] == "INTEGRATED"
