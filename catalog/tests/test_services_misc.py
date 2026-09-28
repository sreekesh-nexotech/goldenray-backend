"""Service-level rules not reached through the API tests: dashboard counters, price labels, spec guards, helpers."""

from decimal import Decimal

import pytest

from catalog.models import ComponentStatus
from catalog.services import brands, pricing_hooks, specs
from catalog.services.categories import attribute_errors, check_attribute_schema
from catalog.services.common import DOCUMENT_RULE, asset_problem, unique_slug
from catalog.services.legacy_support import BadValue, dec, derived_prefix, integer, model_key, moment, snake
from catalog.tests.factories import BatteryFamilyFactory, BrandFactory, CategoryFactory, ComponentFactory, battery, panel, published
from core.errors import DomainError
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db


class TestDashboard:
    def test_counts_per_module(self, auth_client, make_user):
        panel(status=ComponentStatus.DRAFT)
        panel(status=ComponentStatus.RETIRED)
        published(panel())
        user = make_user(grants={"dashboard": ["view"], "catalog": ["view"], "products_public": ["view"]})
        modules = auth_client(user).get("/api/v1/dashboard/").json()["modules"]
        assert modules["catalog"] == {"components": 3, "draft": 1, "active": 1, "deprecated": 0, "retired": 1, "brands": 3, "categories": 1}
        assert modules["products_public"] == {"profiles": 1, "published": 1, "draft": 0}
        only_dashboard = auth_client(make_user(grants={"dashboard": ["view"]})).get("/api/v1/dashboard/").json()["modules"]
        assert "catalog" not in only_dashboard and "products_public" not in only_dashboard


class TestPricingHooks:
    @pytest.mark.parametrize(
        "amount,text",
        [(Decimal("140300"), "₹1,40,300"), (Decimal("28000.00"), "₹28,000"), (Decimal("999"), "₹999"), (Decimal("12345678.5"), "₹1,23,45,678.50"), (Decimal("-1500"), "-₹1,500")],
    )
    def test_format_inr(self, amount, text):
        assert pricing_hooks.format_inr(amount) == text

    def test_price_label(self):
        info = pricing_hooks.PriceInfo
        assert pricing_hooks.price_label(None, "fallback") == "fallback"
        assert pricing_hooks.price_label(info(label="On request"), "x") == "On request"
        assert pricing_hooks.price_label(info(min_amount=Decimal("100"), max_amount=Decimal("100")), "x") == "₹100"
        assert pricing_hooks.price_label(info(max_amount=Decimal("250")), "x") == "₹250"
        assert pricing_hooks.price_label(info(), "x") == "x"

    def test_provider_contract(self):
        component = panel()
        assert pricing_hooks.prices_for([]) == {} and pricing_hooks.prices_for([component]) == {component.pk: None}
        pricing_hooks.register(lambda components: {component.pk: pricing_hooks.PriceInfo(min_amount=Decimal("1"))})
        assert pricing_hooks.prices_for([component])[component.pk].min_amount == Decimal("1")
        assert pricing_hooks.current_provider() is not pricing_hooks.default_provider


class TestSpecGuards:
    def test_unknown_and_deleted(self):
        component = battery()
        with pytest.raises(DomainError) as unknown:
            specs.apply_spec(component, "battery", {"colour": "red"}, user=None)
        assert unknown.value.errors == {"battery_spec": {"colour": ["Not a field of this spec."]}}
        family = BatteryFamilyFactory()
        family.soft_delete()
        with pytest.raises(DomainError):
            specs.apply_spec(component, "battery", {"family": family}, user=None)
        assert specs.get_spec(component, None) is not None and specs.spec_snapshot(None) == {} and specs.spec_kind(None) is None
        assert specs.get_spec(ComponentFactory()) is None  # a MISC category has no spec table


class TestHelpers:
    def test_attribute_schema_helpers(self):
        check_attribute_schema({})
        assert attribute_errors({}, {"any": 1}) == [] and attribute_errors({}, []) == ["Must be a JSON object."]
        assert attribute_errors({"type": "object", "properties": {"a": {"type": "integer"}}}, {"a": "x"}) == ["a: 'x' is not of type 'integer'"]

    def test_brand_helpers(self):
        assert brands.normalise_name("  kanberry  ") == "kanberry" and brands.name_key("Renew  Sys") == "renew sys"
        assert brands.ensure_brand("", user=None) == (None, False) and brands.find_by_name("") is None
        existing = BrandFactory(name="Waaree")
        assert brands.ensure_brand("WAAREE", user=None) == (existing, False)

    def test_asset_rules_and_slugs(self):
        assert asset_problem(None, DOCUMENT_RULE) is None
        deleted = MediaAssetFactory(kind="DOCUMENT")
        deleted.soft_delete()
        assert asset_problem(deleted, DOCUMENT_RULE) == "The file has been deleted."
        assert unique_slug("!!!", lambda slug: slug == "item", fallback="item") == "item-2"

    def test_legacy_value_converters(self):
        assert dec("21.30", "x", places=2, digits=5) == Decimal("21.30") and dec(None, "x", places=2, digits=5) is None
        for value, places, digits in (("1.234", 2, 5), ("123456", 2, 5), ("abc", 2, 5)):
            with pytest.raises(BadValue):
                dec(value, "x", places=places, digits=digits)
        for value in (True, "1.5", "x", -1):
            with pytest.raises(BadValue):
                integer(value, "x")
        assert integer("7", "x") == 7 and model_key("SG 5.0-RS") == "sg50rs" and snake("panelsPerDevice") == "panels_per_device"
        assert moment("not a date") is None and moment("2026-09-09T06:30:38") is not None and moment(None) is None

    def test_derived_prefix_avoids_collisions(self):
        CategoryFactory(sku_prefix="SOLARC")
        assert derived_prefix("solar_clamp") == "SOLAR2"
        assert derived_prefix("9volt") == "C9VOLT"
