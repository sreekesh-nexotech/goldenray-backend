"""catalog/components/ — CRUD with nested spec and tiers, SKU rules, attributes, history, search, query budget."""

from decimal import Decimal

import pytest

from audit.models import AuditLog
from catalog.models import Component, ComponentChange, ComponentStatus, PanelSpec
from catalog.tests.factories import (
    BatteryFamilyFactory,
    BrandFactory,
    CategoryFactory,
    ComponentFactory,
    ComponentTierFactory,
    battery_category,
    inverter,
    inverter_category,
    panel,
    panel_category,
    structure_category,
)
from core.models import OutboxEvent
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/catalog/components/"


def detail(component, suffix=""):
    return f"{URL}{component.uid}/{suffix}"


@pytest.fixture
def panels():
    return panel_category()


def new_panel(category, **extra):
    return {
        "category": str(category.uid),
        "name": "Waaree 540W DCR",
        "model": "WS-540",
        "tiers": ["VALUE", "BASE"],
        "panel_spec": {"wattage_w": 540, "efficiency_pct": "21.36", "certifications": ["BIS"]},
        **extra,
    }


class TestPermissions:
    def test_anonymous_forbidden_and_view_only(self, api_client, outsider, viewer, panels):
        component = panel()
        for method, url in (("get", URL), ("post", URL), ("patch", detail(component)), ("delete", detail(component)), ("post", detail(component, "activate/")), ("get", detail(component, "history/"))):
            assert getattr(api_client, method)(url, {}, format="json").status_code == 401
        assert outsider.get(URL).status_code == 403
        assert viewer.get(URL).status_code == 200 and viewer.get(detail(component, "history/")).status_code == 200 and viewer.get(detail(component, "usage/")).status_code == 200
        assert viewer.post(URL, new_panel(panels), format="json").status_code == 403
        assert viewer.patch(detail(component), {"name": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(component)).status_code == 403
        for action in ("activate", "deprecate", "retire"):
            assert viewer.post(detail(component, f"{action}/"), {"reason": "x"}, format="json").status_code == 403

    def test_create_needs_create_and_edit_needs_edit(self, auth_client, make_user, panels):
        creator = auth_client(make_user(grants={"catalog": ["view", "create"]}))
        response = creator.post(URL, new_panel(panels), format="json")
        assert response.status_code == 201
        assert creator.patch(f"{URL}{response.json()['uid']}/", {"name": "x"}, format="json").status_code == 403

    def test_scope_all(self, auth_client, make_user, panels):
        panel()
        client = auth_client(make_user(grants={"catalog": ["view"]}, scopes={"catalog": "all"}))
        assert client.get(URL).json()["count"] == 1


class TestCreate:
    def test_create_with_spec_tiers_and_generated_sku(self, client, catalog_user, panels):
        brand = BrandFactory(name="Waaree")
        response = client.post(URL, new_panel(panels, brand=str(brand.uid)), format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["sku"] == "PNL-0001" and body["status"] == "DRAFT" and body["brand_label"] == "Waaree" and body["tiers"] == ["BASE", "VALUE"]
        assert body["spec_kind"] == "panel" and body["panel_spec"]["wattage_w"] == 540 and body["panel_spec"]["efficiency_pct"] == "21.36" and body["inverter_spec"] is None
        assert body["effective_gst_rate"] == "0.0500" and body["effective_unit"] == "NOS" and body["version"] == 1
        assert client.post(URL, new_panel(panels), format="json").json()["sku"] == "PNL-0002"
        component = Component.objects.get(sku="PNL-0001")
        assert ComponentChange.objects.get(component=component, field="created").new["spec"]["wattage_w"] == 540
        assert AuditLog.objects.get(action="catalog.component_created", object_uid=component.uid).actor == catalog_user
        assert OutboxEvent.objects.filter(event_type="catalog.component_created").count() == 2

    def test_explicit_sku_is_unique_case_insensitively(self, client, panels):
        panel(sku="p1")
        response = client.post(URL, new_panel(panels, sku="P1"), format="json")
        assert response.status_code == 409 and response.json()["code"] == "sku_taken"
        assert client.post(URL, new_panel(panels, sku="bad sku!"), format="json").json()["errors"]["sku"]

    def test_spec_rules(self, client, panels):
        response = client.post(URL, new_panel(panels, panel_spec={"efficiency_pct": "21.3"}), format="json")
        assert response.status_code == 400 and response.json()["errors"]["panel_spec"]["wattage_w"]
        response = client.post(URL, new_panel(panels, inverter_spec={"kw": "5"}), format="json")
        assert response.status_code == 400 and "inverter_spec" in response.json()["errors"]
        response = client.post(URL, new_panel(panels, panel_spec={"wattage_w": 540, "efficiency_pct": "21.366"}), format="json")
        assert response.status_code == 400 and "efficiency_pct" in response.json()["errors"]["panel_spec"]

    def test_inverter_battery_and_structure_specs(self, client):
        family = BatteryFamilyFactory(slug="seg-lv")
        inv = client.post(
            URL, {"category": str(inverter_category().uid), "name": "Growatt 5k", "inverter_spec": {"kw": "5", "inverter_type": "HYBRID", "compatible_battery_families": ["seg-lv"]}}, format="json"
        )
        assert inv.status_code == 201 and inv.json()["inverter_spec"]["compatible_battery_families"] == ["seg-lv"]
        bad = client.post(
            URL, {"category": str(Component.objects.get().category.uid), "name": "x", "inverter_spec": {"kw": "5", "inverter_type": "ONGRID", "compatible_battery_families": ["nope"]}}, format="json"
        )
        assert bad.status_code == 400 and "compatible_battery_families" in bad.json()["errors"]
        bat = client.post(
            URL, {"category": str(battery_category().uid), "name": "SEG 100Ah", "battery_spec": {"family": str(family.uid), "capacity_kwh": "5.12", "compatible_inverters": None}}, format="json"
        )
        assert bat.status_code == 201 and bat.json()["battery_spec"]["family"]["slug"] == "seg-lv" and bat.json()["battery_spec"]["compatible_inverters"] is None
        tube = client.post(URL, {"category": str(structure_category().uid), "name": "Tube", "structure_spec": {"tube_size": "1.5x1.5", "material": "GP"}}, format="json")
        assert tube.status_code == 201 and tube.json()["structure_spec"]["material"] == "GP"

    def test_attributes_follow_the_category_schema(self, client):
        category = CategoryFactory(attribute_schema={"type": "object", "properties": {"phase": {"enum": ["1P", "3P"]}}, "required": ["phase"]})
        response = client.post(URL, {"category": str(category.uid), "name": "DCDB", "attributes": {"phase": "2P"}}, format="json")
        assert response.status_code == 400 and response.json()["errors"]["attributes"]
        assert client.post(URL, {"category": str(category.uid), "name": "DCDB"}, format="json").status_code == 400
        assert client.post(URL, {"category": str(category.uid), "name": "DCDB", "attributes": {"phase": "1P"}}, format="json").status_code == 201

    def test_inactive_category_and_bad_assets(self, client):
        inactive = CategoryFactory(is_active=False)
        assert client.post(URL, {"category": str(inactive.uid), "name": "x"}, format="json").json()["errors"]["category"]
        category = CategoryFactory()
        image = MediaAssetFactory(kind="DOCUMENT", mime_type="application/pdf")
        response = client.post(URL, {"category": str(category.uid), "name": "x", "primary_image": str(image.uid), "datasheet": str(MediaAssetFactory().uid)}, format="json")
        assert response.status_code == 400 and set(response.json()["errors"]) == {"primary_image", "datasheet"}

    def test_validation_envelope(self, client):
        response = client.post(URL, {"name": ""}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and {"category", "name"} <= set(response.json()["errors"])


class TestUpdate:
    def test_patch_fields_spec_and_tiers(self, client):
        component = panel(status=ComponentStatus.DRAFT)
        ComponentTierFactory(component=component, tier="BASE")
        response = client.patch(detail(component), {"name": "New name", "tiers": ["PREMIUM"], "panel_spec": {"efficiency_pct": "22.10"}, "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["name"] == "New name" and body["tiers"] == ["PREMIUM"] and body["panel_spec"]["efficiency_pct"] == "22.10" and body["version"] == 2
        fields = set(ComponentChange.objects.filter(component=component).values_list("field", flat=True))
        assert {"name", "tiers", "spec.efficiency_pct"} <= fields
        audit = AuditLog.objects.get(action="catalog.component_updated")
        assert audit.after["tiers"] == ["PREMIUM"] and audit.after["spec"] == {"efficiency_pct": "22.10"}

    def test_nested_only_change_bumps_version_and_noop_does_not(self, client):
        component = panel()
        assert client.patch(detail(component), {"panel_spec": {"wattage_w": 545}}, format="json").json()["version"] == 2
        assert client.patch(detail(component), {"panel_spec": {"wattage_w": 545}, "name": component.name}, format="json").json()["version"] == 2
        assert PanelSpec.objects.get(component=component).wattage_w == 545

    def test_restored_tier_row_is_reused(self, client):
        component = panel()
        client.patch(detail(component), {"tiers": ["BASE"]}, format="json")
        client.patch(detail(component), {"tiers": []}, format="json")
        client.patch(detail(component), {"tiers": ["BASE"]}, format="json")
        assert component.tiers.model.all_objects.filter(component=component).count() == 1

    def test_sku_locked_after_draft(self, client):
        draft = ComponentFactory(status=ComponentStatus.DRAFT, sku="d1")
        assert client.patch(detail(draft), {"sku": "d2"}, format="json").json()["sku"] == "d2"
        active = ComponentFactory(sku="a1")
        response = client.patch(detail(active), {"sku": "a2"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "sku_locked"
        ComponentFactory(sku="taken", status=ComponentStatus.DRAFT)
        assert client.patch(detail(draft), {"sku": "TAKEN"}, format="json").json()["code"] == "sku_taken"

    def test_brand_change_relabels_unless_label_given(self, client):
        component = ComponentFactory(brand=BrandFactory(name="Old"), brand_label="Old")
        new = BrandFactory(name="New")
        assert client.patch(detail(component), {"brand": str(new.uid)}, format="json").json()["brand_label"] == "New"
        assert client.patch(detail(component), {"brand": None}, format="json").json()["brand_label"] == ""
        assert client.patch(detail(component), {"brand": str(new.uid), "brand_label": "NEW Ltd"}, format="json").json()["brand_label"] == "NEW Ltd"

    def test_category_change_across_spec_tables_is_refused(self, client):
        component = panel()
        response = client.patch(detail(component), {"category": str(inverter_category().uid)}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "spec_kind_change"
        other_panels = panel_category(slug="panel-2", sku_prefix="PNLB")
        assert client.patch(detail(component), {"category": str(other_panels.uid)}, format="json").status_code == 200

    def test_spec_required_field_cannot_be_nulled(self, client):
        component = inverter()
        response = client.patch(detail(component), {"inverter_spec": {"kw": None}}, format="json")
        assert response.status_code == 400

    def test_stale_version(self, client):
        component = panel(version=5)
        response = client.patch(detail(component), {"name": "x", "expected_version": 4}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"


class TestDeleteHistoryAndList:
    def test_delete_soft_deletes_component_and_profile(self, client):
        from catalog.tests.factories import published

        component = panel()
        profile = published(component)
        stale = client.delete(f"{detail(component)}?expected_version=9")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        assert client.delete(f"{detail(component)}?expected_version=1").status_code == 204
        component.refresh_from_db()
        profile.refresh_from_db()
        assert component.deleted_at and profile.deleted_at
        assert client.get(detail(component)).status_code == 404
        assert AuditLog.objects.filter(action="catalog.component_deleted").count() == 1

    def test_history_is_cursor_paginated_newest_first(self, client):
        component = panel()
        for index in range(3):
            client.patch(detail(component), {"name": f"Name {index}"}, format="json")
        body = client.get(detail(component, "history/"), {"page_size": 2}).json()
        assert [row["new"] for row in body["results"]] == ["Name 2", "Name 1"] and body["next"] and body["results"][0]["by"]["uid"]
        assert client.get(body["next"]).json()["results"][0]["new"] == "Name 0"

    def test_history_query_budget(self, client, django_assert_max_num_queries):
        component = panel()
        for index in range(20):
            client.patch(detail(component), {"name": f"Name {index}", "tiers": ["BASE"] if index % 2 else []}, format="json")
        with django_assert_max_num_queries(8):
            assert len(client.get(detail(component, "history/"), {"page_size": 50}).json()["results"]) == ComponentChange.objects.filter(component=component).count() == 39

    def test_filters_search_and_ordering(self, client):
        waaree = BrandFactory(name="Waaree", slug="waaree")
        panel(sku="p1", name="Waaree Mono perc 540W", brand=waaree, brand_label="Waaree", status=ComponentStatus.ACTIVE)
        panel(sku="p2", name="Adani Topcon 620W", status=ComponentStatus.RETIRED)
        inverter(sku="i1", name="Growatt MIC 3000TL-X", model="MIC 3000TL-X")
        assert [row["sku"] for row in client.get(URL, {"search": "waar 540"}).json()["results"]] == ["p1"]
        assert [row["sku"] for row in client.get(URL, {"search": "3000tl"}).json()["results"]] == ["i1"]
        assert client.get(URL, {"search": "!!"}).json()["count"] == 3
        assert [row["sku"] for row in client.get(URL, {"filter[category]": "panel", "status": "RETIRED"}).json()["results"]] == ["p2"]
        assert [row["sku"] for row in client.get(URL, {"brand": "waaree"}).json()["results"]] == ["p1"]
        assert [row["sku"] for row in client.get(URL, {"ordering": "-sku"}).json()["results"]] == ["p2", "p1", "i1"]
        ComponentTierFactory(component=Component.objects.get(sku="p1"), tier="PREMIUM")
        assert [row["sku"] for row in client.get(URL, {"tier": "PREMIUM"}).json()["results"]] == ["p1"]

    def test_list_query_budget(self, client, django_assert_max_num_queries):
        for _ in range(15):
            component = panel(datasheet=MediaAssetFactory(kind="DOCUMENT"), primary_image=MediaAssetFactory())
            ComponentTierFactory(component=component)
            inverter()
        with django_assert_max_num_queries(12):
            body = client.get(URL, {"page_size": 100}).json()
        assert body["count"] == 30 and all(row["panel_spec"] or row["inverter_spec"] for row in body["results"])

    def test_decimal_spec_values_round_trip(self, client):
        component = inverter(spec={"kw": Decimal("0.475")})
        assert client.get(detail(component)).json()["inverter_spec"]["kw"] == "0.475"
