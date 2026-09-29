"""POST /api/public/v1/bom/quote/ — shape, no caching, throttle scope, validation envelope, every DomainError, and the
calculator branches on a small hand-built configuration."""

from datetime import date
from decimal import Decimal

import pytest

from bom.services.website_quote import Calculator, QuoteInvalid, load_snapshot, parse_request, quote
from bom.tests.factories import FixedItemFactory, SlotFactory, TemplateFactory
from catalog.models import ComponentStatus
from catalog.tests.factories import CategoryFactory, ComponentFactory, ComponentTierFactory
from pricing.tests.factories import CostConfigFactory, MarketRateFactory, PriceFactory

pytestmark = pytest.mark.django_db
URL = "/api/public/v1/bom/quote/"
TODAY = date(2026, 9, 28)


def _body(**extra):
    return {"sys_type": "ongrid", "size": "3", "tier": "base", **extra}


class TestEndpoint:
    def test_shape_no_store_and_anonymous(self, api_client, world):
        response = api_client.post(URL, _body(), format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert list(body) == ["bom_lines", "pricing", "meta", "available_offers"]  # business default B-1: no cost_breakdown/totals
        assert list(quote(_body(), today=date.today())) == ["bom_lines", "cost_breakdown", "totals", "pricing", "meta", "available_offers"]
        assert list(body["bom_lines"][0]) == ["pos", "name", "brand", "qty", "unit", "unit_price", "amount", "gst_pct", "gst_amt", "section", "is_variable"]
        assert response["Cache-Control"] == "no-store"
        assert body["meta"] == {"system_type": "ongrid", "size": "3", "tier": "base", "bat_config": "0", "is_three_phase": False, "new_panels": None}

    def test_throttle_scope_is_public_write(self, api_client, world, settings):
        from bom.views.quote import QuoteView

        view = QuoteView()
        assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "POST"})()) == "public_write"
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_write": "2/min"}}
        assert [api_client.post(URL, _body(), format="json").status_code for _ in range(3)] == [200, 200, 429]

    def test_get_is_not_allowed(self, api_client):
        assert api_client.get(URL).status_code == 405

    @pytest.mark.parametrize(
        "body,fields",
        [
            ({}, {"sys_type", "tier", "size"}),
            (_body(custom_discount=7500), {"custom_discount"}),
            (_body(custom_discount={"type": "flat", "value": "abc"}), {"custom_discount"}),
            (_body(dist_km="12.5"), {"dist_km"}),
            (_body(ghs_houses="many"), {"ghs_houses"}),
            (_body(margin_val="nan"), {"margin_val"}),
            (_body(dist_km=10**400), {"dist_km"}),
            (_body(sys_type=["ongrid"]), {"sys_type"}),
            (_body(sys_type="upgrade", upgrade_sections=[1]), {"upgrade_sections"}),
            (_body(sys_type="upgrade", upgrade_to_kw="x"), {"upgrade_to_kw"}),
            (_body(size="nan"), {"size"}),
            (_body(size="inf"), {"size"}),
            (_body(size="-1e400"), {"size"}),
            (_body(size="1e13"), {"size"}),
        ],
    )
    def test_validation_envelope(self, api_client, world, body, fields):
        response = api_client.post(URL, body, format="json")
        assert response.status_code == 400
        payload = response.json()
        assert payload["code"] == "validation_error" and set(payload["errors"]) == fields

    @pytest.mark.parametrize("offer_id", [{"a": 1}, [1], 5])
    def test_a_non_string_offer_id_matches_no_offer_as_before(self, api_client, world, offer_id):
        # the legacy view compared it with every offer id and fell through to no discount (HTTP 200)
        response = api_client.post(URL, _body(selected_offer_id=offer_id), format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["pricing"]["discount_amt"] == 0 and response.json()["pricing"]["discount_label"] == ""

    def test_non_object_body(self, api_client, world):
        response = api_client.post(URL, [1, 2], format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"

    def test_missing_template_and_configuration(self, api_client, world):
        response = api_client.post(URL, _body(sys_type="hybrid"), format="json")
        assert response.status_code == 400 and response.json()["code"] == "bom_template_missing"
        from pricing.models import CostConfig

        CostConfig.objects.filter(key="install_rate").delete()
        response = api_client.post(URL, _body(), format="json")
        assert response.status_code == 503 and response.json()["code"] == "quote_not_configured"


class TestCalculator:
    def test_lines_costs_and_totals(self, world):
        result = quote(_body(subsidy_type="residential"), today=TODAY)
        names = [line["name"] for line in result["bom_lines"]]
        assert names == ["Panel 540", "Inverter 3", "Cable 4sqmm", "MC4"]  # 3P-only item skipped by its condition, tube never a line
        panel, inverter, cable, mc4 = result["bom_lines"]
        assert panel["qty"] == 6 and panel["unit_price"] == 13122.0 and panel["gst_pct"] == 5 and panel["gst_amt"] == round(6 * 13122.0 * 5 / 100)
        assert inverter["gst_pct"] == 5  # the slot has no rate: the category's
        assert cable["unit"] == "m" and mc4["pos"] == 500 and mc4["section"] == "misc" and mc4["brand"] == ""
        assert result["cost_breakdown"]["structure_base"] == round(15.3 * 85 * 3) and result["cost_breakdown"]["structure_extra"] == 0
        assert result["pricing"]["subsidy_amt"] == 78000 and result["pricing"]["subsidy_label"] == "PM Surya Ghar (Residential, 3.0kW)"
        assert result["pricing"]["market_rate"] == 0 and result["pricing"]["customer_price"] == result["totals"]["grand_total"]
        assert [offer["offer_id"] for offer in result["available_offers"]] == ["FLAT5K", "PCT3"]

    def test_three_phase_size_and_nearest_inverter(self, world):
        result = quote(_body(size="5tp", tier="value"), today=TODAY)
        names = [line["name"] for line in result["bom_lines"]]
        assert "Inverter 5" in names and "3P only" in names and "Cable 4sqmm" not in names
        assert result["meta"]["is_three_phase"] is True
        assert [offer["offer_id"] for offer in result["available_offers"]] == ["FLAT5K", "SIZE5"]

    def test_market_rate_offers_and_discounts(self, world):
        flat = quote(_body(tier="value", selected_offer_id="FLAT5K"), today=TODAY)["pricing"]
        assert flat["market_rate"] == 230000.0 and flat["discount_amt"] == 5000 and flat["discount_label"] == "Flat 5k" and flat["final_price"] == 225000.0
        percent = quote(_body(selected_offer_id="PCT3"), today=TODAY)["pricing"]
        assert percent["discount_amt"] == round(percent["customer_price"] * 3.0 / 100)
        expired = quote(_body(selected_offer_id="OLD"), today=TODAY)["pricing"]
        assert expired["discount_amt"] == 0 and expired["discount_label"] == ""
        custom = quote(_body(custom_discount={"type": "percent", "value": 5}), today=TODAY)["pricing"]
        assert custom["discount_label"] == "Custom 5.0% discount"
        rupees = quote(_body(custom_discount={"type": "flat", "value": 12345}), today=TODAY)["pricing"]
        assert rupees["discount_label"] == "Custom ₹12,345 discount" and rupees["discount_amt"] == 12345

    def test_margin_subsidy_structure_and_distance(self, world):
        result = quote(_body(margin_type="flat", margin_val=15000, subsidy_type="ghs", ghs_houses=3, structure_type="elevated", dist_km=175), today=TODAY)
        costs = result["cost_breakdown"]
        assert result["totals"]["margin"] == 15000
        assert result["pricing"]["subsidy_amt"] == 3.0 * 18000 * 3 and result["pricing"]["subsidy_label"] == "PM Surya Ghar (GHS, 3 houses)"
        extra = round(15.3 * 85 * 5) + round(80.0 * 4) - round(15.3 * 85 * 3)
        assert costs["structure_extra"] == extra and costs["structure_labor"] == 1500 and costs["structure_repair"] == round((extra + 1500.0) * 10.0 / 100)
        assert costs["transport"] == round(175 * 40.0 + 75 * 35)
        sheet = quote(_body(structure_type="sheetRoof"), today=TODAY)["cost_breakdown"]
        assert sheet["structure_extra"] == 0 and sheet["structure_labor"] == 1500  # no sheet_roof template: 0 kg

    def test_extra_km_rate_comes_from_cost_config(self, world):
        CostConfigFactory(key="transport_extra_rate_per_km", value=50)
        assert quote(_body(dist_km=110), today=TODAY)["cost_breakdown"]["transport"] == round(110 * 40.0 + 10 * 50)

    def test_upgrade(self, world):
        upgrade = TemplateFactory(system_type="UPGRADE", name="Upgrade", sizes=[], three_phase_sizes=[])
        panels = world.slots.get(key="panel").category
        SlotFactory(template=upgrade, key="panel", category=panels, qty_rule={"type": "new_panels"}, gst_rate=Decimal("0.05"))
        inverters = world.slots.get(key="inverter").category
        SlotFactory(template=upgrade, key="inverter", category=inverters, qty_rule={"type": "fixed", "qty": 1}, filter_type="ONGRID")
        connectors = CategoryFactory(slug="mc4_connector")
        mc4 = ComponentFactory(category=connectors, name="MC4 Pair", status=ComponentStatus.ACTIVE)
        PriceFactory(component=mc4, amount=Decimal("56.00"))
        FixedItemFactory(template=upgrade, name="MC4 row", component=mc4, category=connectors, unit_price=None, qty_rule={"type": "upgrade_path", "qty": {"3_5": 3}})
        FixedItemFactory(template=upgrade, name="No component", unit_price=Decimal("5.00"), qty_rule={"type": "upgrade_path", "qty": {"3_5": 3}})
        MarketRateFactory(
            set=world_rates(), system_type="UPGRADE", tier="", size_key="5", from_size_key="3", from_size_kw=Decimal("3"), size_kw=Decimal("5"), customer_price_incl_gst=Decimal("90000.00")
        )
        result = quote({"sys_type": "upgrade", "size": "5", "tier": "base", "upgrade_from_kw": 3, "upgrade_to_kw": 5}, today=TODAY)
        assert [line["name"] for line in result["bom_lines"]] == ["Panel 540", "Inverter 5", "MC4 Pair"]
        assert result["bom_lines"][0]["qty"] == 4 and result["bom_lines"][2]["qty"] == 3 and result["bom_lines"][2]["section"] == "wiring"
        assert result["meta"] == {"system_type": "upgrade", "size": "3.0→5.0", "tier": "base", "bat_config": "0", "is_three_phase": True, "new_panels": 4}
        assert result["pricing"]["market_rate"] == 90000.0 and result["cost_breakdown"]["service"] == 0
        hybrid = quote({"sys_type": "upgrade", "size": "5", "tier": "base", "upgrade_sections": {"panels": False, "hybrid_inv": True, "wiring": False}}, today=TODAY)
        # hybrid_inv switches the inverter slot on (its own filter still applies); wiring off drops the MC4 row
        assert [(line["name"], line["section"]) for line in hybrid["bom_lines"]] == [("Inverter 5", "inverter")]

    def test_upgrade_fixed_item_without_gst_falls_back_like_the_legacy(self, world):
        # legacy: ``fi.gst or (cat.gst_default if cat else 18)`` with ``cat`` looked up among the slot categories only
        upgrade = TemplateFactory(system_type="UPGRADE", name="Upgrade", sizes=[], three_phase_sizes=[])
        panels = world.slots.get(key="panel").category
        SlotFactory(template=upgrade, key="panel", category=panels, qty_rule={"type": "new_panels"}, gst_rate=Decimal("0.05"))
        clamps = CategoryFactory(slug="solar_clamp", gst_rate=Decimal("0.12"))
        clamp = ComponentFactory(category=clamps, name="Clamp", status=ComponentStatus.ACTIVE)
        PriceFactory(component=clamp, amount=Decimal("10.00"))
        panel = ComponentFactory(category=panels, name="Panel row", status=ComponentStatus.ACTIVE)
        PriceFactory(component=panel, amount=Decimal("10.00"))
        rule = {"type": "upgrade_path", "qty": {"3_5": 2}}
        FixedItemFactory(template=upgrade, name="zero, slot category", component=panel, category=panels, unit_price=None, gst_rate=Decimal("0"), section="panels", qty_rule=rule)
        FixedItemFactory(template=upgrade, name="zero, other category", component=clamp, category=clamps, unit_price=None, gst_rate=Decimal("0"), section="panels", qty_rule=rule)
        lines = quote({"sys_type": "upgrade", "size": "5", "tier": "base", "upgrade_from_kw": 3, "upgrade_to_kw": 5}, today=TODAY)["bom_lines"]
        gst = {line["name"]: line["gst_pct"] for line in lines if not line["is_variable"]}
        assert gst == {"Panel row": 5, "Clamp": 18}

    def test_request_coercion_matches_the_legacy_defaults(self):
        params = parse_request({"sys_type": "ongrid", "size": 3, "tier": "base", "dist_km": 12.9, "ghs_houses": 0, "bat_config": None})
        assert params["size"] == "3" and params["dist_km"] == 12 and params["ghs_houses"] == 1 and params["bat_config"] == "None"
        assert params["margin_val"] == 20.0 and params["upgrade_sections"]["wiring"] is True and params["custom_discount"] is None
        with pytest.raises(QuoteInvalid) as caught:
            parse_request({"sys_type": "x", "tier": "value", "size": "3"})
        assert caught.value.legacy_errors == ["sys_type must be ongrid | hybrid | upgrade"]

    def test_draft_components_and_components_without_price_are_not_quoted(self, world):
        panels = world.slots.get(key="panel").category
        draft = ComponentFactory(category=panels, name="Draft panel", status=ComponentStatus.DRAFT)
        ComponentTierFactory(component=draft, tier="BASE")
        PriceFactory(component=draft, amount=Decimal("1.00"))
        snapshot = load_snapshot("ongrid")
        names = [item.name for slot in snapshot.slots for item in slot.category.items]
        assert "Draft panel" not in names
        assert Calculator(snapshot, parse_request(_body()), TODAY).compute()["bom_lines"][0]["name"] == "Panel 540"


def world_rates():
    from pricing.models import MarketRateSet

    return MarketRateSet.objects.get(status="ACTIVE")
