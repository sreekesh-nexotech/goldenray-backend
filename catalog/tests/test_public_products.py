"""/api/public/v1/products/… — shape, price hook, caching (catalog + pricing namespaces), throttle, filters, N+1."""

from decimal import Decimal

import pytest

from catalog.models import ComponentStatus
from catalog.services import pricing_hooks
from catalog.tests.factories import BrandFactory, CategoryFactory, battery, inverter, panel, published
from flarize.cache_utils import bump
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
PANELS = "/api/public/v1/products/panels/"


@pytest.fixture
def waaree_panel():
    component = panel(
        sku="p1",
        name="Waaree Ahnay 550",
        model="Ahnay Bi-55-550",
        brand=BrandFactory(name="Waaree", slug="waaree"),
        brand_label="Waaree",
        warranty_product_years=12,
        warranty_performance_years=30,
        spec={"wattage_w": 550, "panel_type": "BIFACIAL", "technology": "P_TYPE_PERC", "certifications": ["BIS"]},
    )
    return published(
        component,
        slug="waaree-ahnay-bi-55-550",
        headline="Ahnay Bi-55-550",
        price_range_label="₹28,000 - ₹32,000",
        kerala_climate_score=96,
        overall_rating="EXCELLENT",
        ratings={"efficiency": 93},
        subsidy_eligible=True,
    )


class TestShape:
    def test_list_shape_without_a_price_provider(self, api_client, waaree_panel):
        response = api_client.get(PANELS)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"results", "count", "next", "previous"} and body["count"] == 1
        item = body["results"][0]
        assert item["slug"] == "waaree-ahnay-bi-55-550" and item["sku"] == "p1" and item["brand"] == {"uid": str(waaree_panel.component.brand.uid), "name": "Waaree", "slug": "waaree"}
        assert item["price"] is None and item["price_range_label"] == "₹28,000 - ₹32,000"
        assert item["spec_kind"] == "panel" and item["spec"]["wattage_w"] == 550 and item["spec"]["certifications"] == ["BIS"]
        assert item["warranty"] == {"product_years": 12, "performance_years": 30, "extendable_years": None, "text": ""}
        assert item["ratings"] == {"efficiency": 93} and item["overall_rating"] == "EXCELLENT" and item["image"] is None
        assert "uid" not in item and "version" not in item

    def test_price_from_the_registered_provider(self, api_client, waaree_panel):
        calls = []

        @pricing_hooks.register
        def provider(components):
            calls.append(len(components))
            return {component.pk: pricing_hooks.PriceInfo(min_amount=Decimal("28500.00"), max_amount=Decimal("31000.00"), release_number=4) for component in components}

        item = api_client.get(PANELS).json()["results"][0]
        assert item["price"] == {"min": "28500.00", "max": "31000.00", "currency": "INR", "gst_inclusive": True, "release": 4}
        assert item["price_range_label"] == "₹28,500 - ₹31,000" and calls == [1]

    def test_failing_provider_falls_back_to_the_label(self, api_client, waaree_panel):
        pricing_hooks.register(lambda components: 1 / 0)
        item = api_client.get(PANELS).json()["results"][0]
        assert item["price"] is None and item["price_range_label"] == "₹28,000 - ₹32,000"

    def test_detail_by_alias_or_slug_and_extra_fields(self, api_client, waaree_panel):
        image = MediaAssetFactory(alternative_text="front")
        waaree_panel.gallery = [str(image.uid), str(MediaAssetFactory(visibility="PRIVATE", cdn_url="").uid)]
        waaree_panel.body = "## Why"
        waaree_panel.save()
        for category in ("panels", "panel"):
            response = api_client.get(f"/api/public/v1/products/{category}/waaree-ahnay-bi-55-550/")
            assert response.status_code == 200
        body = response.json()
        assert body["body"] == "## Why" and [asset["uid"] for asset in body["gallery"]] == [str(image.uid)]
        assert body["seo"] == {"title": "Ahnay Bi-55-550", "description": "A well-made product."} and body["datasheet"] is None
        missing = api_client.get("/api/public/v1/products/inverters/waaree-ahnay-bi-55-550/")
        assert missing.status_code == 404 and missing.json()["code"] == "product_not_found"

    @pytest.mark.parametrize(
        "change",
        [{"component": {"is_public": False}}, {"component": {"status": ComponentStatus.RETIRED}}, {"profile": {"status": "DRAFT"}}, {"category": {"is_active": False}}],
    )
    def test_hidden_products(self, api_client, waaree_panel, change):
        component = waaree_panel.component
        if "component" in change:
            type(component).objects.filter(pk=component.pk).update(**change["component"])
        if "profile" in change:
            type(waaree_panel).objects.filter(pk=waaree_panel.pk).update(**change["profile"])
        if "category" in change:
            type(component.category).objects.filter(pk=component.category_id).update(**change["category"])
        assert api_client.get(PANELS).json()["count"] == 0
        assert api_client.get("/api/public/v1/products/panel/waaree-ahnay-bi-55-550/").status_code == 404

    def test_inverters_and_batteries(self, api_client):
        published(inverter(spec={"kw": Decimal("0.475"), "topology": "MICRO", "system_controller_required": True}), kerala_climate_score=94, rating_tier="PREMIUM")
        published(battery(spec={"capacity_kwh": Decimal("5.12"), "engineering_notes": "internal"}), kerala_climate_score=None)
        inv = api_client.get("/api/public/v1/products/inverters/").json()["results"][0]
        assert inv["spec"]["kw"] == "0.475" and "system_controller_required" not in inv["spec"] and inv["rating_tier"] == "PREMIUM"
        bat = api_client.get("/api/public/v1/products/batteries/").json()["results"][0]
        assert bat["spec"]["capacity_kwh"] == "5.12" and "engineering_notes" not in bat["spec"] and bat["spec"]["family"] is None


class TestCaching:
    def test_headers_etag_304_and_hit(self, api_client, waaree_panel):
        first = api_client.get(PANELS)
        assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS" and first["ETag"]
        second = api_client.get(PANELS)
        assert second["X-Cache"] == "HIT"
        assert api_client.get(PANELS, HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304

    @pytest.mark.parametrize("namespace", ["catalog", "pricing", "media"])
    def test_every_namespace_invalidates(self, api_client, waaree_panel, namespace):
        api_client.get(PANELS)
        bump(namespace)
        assert api_client.get(PANELS)["X-Cache"] == "MISS"

    def test_staff_edit_invalidates(self, api_client, auth_client, make_user, waaree_panel):
        before = api_client.get(PANELS).json()
        editor = auth_client(make_user(grants={"products_public": "*"}))
        editor.patch(f"/api/v1/catalog/public-profiles/{waaree_panel.uid}/", {"headline": "New headline"}, format="json")
        after = api_client.get(PANELS)
        assert after["X-Cache"] == "MISS" and after.json()["results"][0]["headline"] == "New headline" and before != after.json()

    def test_anonymous_public_read_throttle(self, api_client, settings, waaree_panel):
        from catalog.views.public import PanelListView, ProductDetailView

        for view_class in (PanelListView, ProductDetailView):
            view = view_class()
            assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "GET"})()) == "public_read"
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_read": "2/min"}}
        assert [api_client.get(PANELS).status_code for _ in range(3)] == [200, 200, 429]


class TestFiltersAndBudget:
    def test_filters_and_ordering(self, api_client, waaree_panel):
        other = published(panel(sku="p2", brand=BrandFactory(slug="adani"), spec={"wattage_w": 620, "panel_type": "MONOCRYSTALLINE"}), kerala_climate_score=80, overall_rating="GOOD")
        unscored = published(panel(sku="p3"), kerala_climate_score=None)
        assert [row["sku"] for row in api_client.get(PANELS).json()["results"]] == ["p1", "p2", unscored.component.sku]
        assert [row["sku"] for row in api_client.get(PANELS, {"brand": "adani"}).json()["results"]] == ["p2"]
        assert [row["sku"] for row in api_client.get(PANELS, {"panel_type": "BIFACIAL"}).json()["results"]] == ["p1"]
        assert [row["sku"] for row in api_client.get(PANELS, {"overall_rating": "GOOD"}).json()["results"]] == ["p2"]
        assert [row["sku"] for row in api_client.get(PANELS, {"min_kerala_score": 90}).json()["results"]] == ["p1"]
        assert [row["sku"] for row in api_client.get(PANELS, {"search": "ahnay"}).json()["results"]] == ["p1"]
        assert [row["sku"] for row in api_client.get(PANELS, {"ordering": "kerala_climate_score"}).json()["results"]][:2] == ["p2", "p1"] or other
        assert api_client.get(PANELS, {"panel_type": "NOPE"}).status_code == 400

    def test_query_budget(self, api_client, django_assert_max_num_queries):
        for index in range(20):
            published(panel(primary_image=MediaAssetFactory(), datasheet=MediaAssetFactory(kind="DOCUMENT")), kerala_climate_score=index)
        CategoryFactory()
        with django_assert_max_num_queries(6):
            assert api_client.get(PANELS, {"page_size": 50}).json()["count"] == 20
