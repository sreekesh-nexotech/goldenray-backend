"""bom/templates|slots|fixed-items|structure-templates|structure-items|tube-weights|package-profiles/ CRUD."""

from decimal import Decimal

import pytest

from audit.models import AuditLog
from bom.models import FixedItem, Slot, StructureTemplateItem, Template
from bom.tests.factories import (
    FixedItemFactory,
    PackageProfileFactory,
    SlotFactory,
    StructureItemFactory,
    StructureTemplateFactory,
    TemplateFactory,
    TubeWeightFactory,
)
from catalog.models import ComponentStatus
from catalog.tests.factories import CategoryFactory, ComponentFactory
from core.models import OutboxEvent
from flarize.cache_utils import get_versions

pytestmark = pytest.mark.django_db
BASE = "/api/v1/bom/"


def _template_body():
    return {
        "system_type": "HYBRID",
        "name": "Hybrid",
        "sizes": [{"key": "3", "label": "3 kW"}, {"key": "8", "label": "8 kW"}],
        "three_phase_sizes": ["8"],
        "tiers": ["base"],
        "battery_configs": ["0", "1"],
    }


def _slot_body():
    return {"template_uid": str(TemplateFactory().uid), "key": "panel", "category_uid": str(CategoryFactory().uid), "qty_rule": {"type": "size_table", "qty": {"3": 6}}, "gst_rate": "0.05"}


def _fixed_body():
    return {"template_uid": str(TemplateFactory().uid), "name": "MC4 Connector", "unit_price": "56.00", "gst_rate": "0.18", "qty_rule": {"type": "size_table", "qty": {"3": 3}}}


def _structure_item_body():
    return {
        "template_uid": str(StructureTemplateFactory().uid),
        "name": "2x1 Tube",
        "item_type": "TUBE",
        "tube_size": "2x1",
        "weight_kg": "10.2",
        "qty_rule": {"type": "kw_interpolated", "points": {"3": 5}},
    }


RESOURCES = {
    "templates": (TemplateFactory, _template_body, {"name": "Renamed"}),
    "slots": (SlotFactory, _slot_body, {"label": "Renamed"}),
    "fixed-items": (FixedItemFactory, _fixed_body, {"name": "Renamed"}),
    "structure-templates": (StructureTemplateFactory, lambda: {"slug": "flat_roof", "name": "Flat Roof"}, {"name": "Renamed"}),
    "structure-items": (StructureItemFactory, _structure_item_body, {"name": "Renamed"}),
    "tube-weights": (TubeWeightFactory, lambda: {"tube_size": "3x1.5", "weight_kg": "19"}, {"weight_kg": "19.5"}),
    "package-profiles": (PackageProfileFactory, lambda: {"key": "ongrid_base", "label": "On-Grid Base", "structure_material": "GP", "inverter_type": "ONGRID"}, {"label": "Renamed"}),
}
NAMES = list(RESOURCES)


@pytest.mark.parametrize("name", NAMES)
def test_permissions(name, api_client, outsider, viewer):
    factory, body, _ = RESOURCES[name]
    row = factory()
    url = f"{BASE}{name}/"
    assert api_client.get(url).status_code == 401
    assert outsider.get(url).status_code == 403
    assert outsider.get(f"{url}{row.uid}/").status_code == 403
    assert viewer.get(url).status_code == 200
    assert viewer.get(f"{url}{row.uid}/").status_code == 200
    assert viewer.post(url, body(), format="json").status_code == 403
    assert viewer.patch(f"{url}{row.uid}/", {}, format="json").status_code == 403
    assert viewer.delete(f"{url}{row.uid}/").status_code == 403
    assert viewer.put(f"{url}{row.uid}/", {}, format="json").status_code in (403, 405)


@pytest.mark.parametrize("name", NAMES)
def test_crud_happy_path_audits_bumps_and_emits(name, client):
    _, body, patch = RESOURCES[name]
    url = f"{BASE}{name}/"
    before = get_versions(["bom"])["bom"]
    created = client.post(url, body(), format="json")
    assert created.status_code == 201, created.json()
    uid = created.json()["uid"]
    assert client.get(f"{url}{uid}/").json()["version"] == 1
    updated = client.patch(f"{url}{uid}/", {**patch, "expected_version": 1}, format="json")
    assert updated.status_code == 200, updated.json()
    assert updated.json()["version"] == 2
    for key, value in patch.items():
        got = updated.json()[key]
        assert (Decimal(got) == Decimal(value)) if key == "weight_kg" else got == value
    stale = client.patch(f"{url}{uid}/", {**patch, "expected_version": 1}, format="json")
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    assert client.delete(f"{url}{uid}/?expected_version=1").status_code == 409
    assert client.delete(f"{url}{uid}/").status_code == 204
    assert client.get(f"{url}{uid}/").status_code == 404
    noun = {"templates": "template", "slots": "slot", "fixed-items": "fixed_item", "structure-templates": "structure_template", "structure-items": "structure_item"}.get(
        name, name[:-1].replace("-", "_")
    )
    assert set(AuditLog.objects.filter(action__startswith=f"bom.{noun}_").values_list("action", flat=True)) == {f"bom.{noun}_created", f"bom.{noun}_updated", f"bom.{noun}_deleted"}
    assert OutboxEvent.objects.filter(event_type="bom.configuration_changed", payload__object_uid=uid).count() == 3
    assert get_versions(["bom"])["bom"] > before


@pytest.mark.parametrize("name", NAMES)
def test_list_query_budget_and_scope_all(name, client, django_assert_max_num_queries):
    factory, _, _ = RESOURCES[name]
    rows = [factory(system_type=system) for system in ("ONGRID", "HYBRID", "UPGRADE")] if name == "templates" else [factory() for _ in range(6)]
    rows[0].soft_delete()
    with django_assert_max_num_queries(8):
        response = client.get(f"{BASE}{name}/", {"page_size": 50})
    # bom has only the `all` scope: every live row, never a deleted one
    assert response.status_code == 200 and response.json()["count"] == len(rows) - 1


@pytest.mark.parametrize("name", NAMES)
def test_validation_envelope(name, client):
    response = client.post(f"{BASE}{name}/", {}, format="json")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error" and set(body) >= {"code", "message", "errors", "error_codes"}


def test_unknown_uid_is_404(client):
    assert client.get(f"{BASE}templates/00000000-0000-0000-0000-000000000000/").status_code == 404


class TestRules:
    def test_template_system_type_is_unique(self, client):
        TemplateFactory(system_type="HYBRID")
        response = client.post(f"{BASE}templates/", {"system_type": "HYBRID", "name": "Again"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "template_exists"

    @pytest.mark.parametrize(
        "patch,field",
        [
            ({"sizes": [{"key": "3"}]}, "sizes"),
            ({"sizes": [{"key": "3", "label": "a"}, {"key": "3", "label": "b"}]}, "sizes"),
            ({"three_phase_sizes": ["9"]}, "three_phase_sizes"),
            ({"tiers": ["gold"]}, "tiers"),
            ({"battery_configs": ["7"]}, "battery_configs"),
        ],
    )
    def test_template_documents(self, client, patch, field):
        template = TemplateFactory()
        response = client.patch(f"{BASE}templates/{template.uid}/", patch, format="json")
        assert response.status_code == 400 and field in response.json()["errors"]

    def test_deleting_a_template_deletes_its_children(self, client):
        slot = SlotFactory()
        item = FixedItemFactory(template=slot.template)
        assert client.delete(f"{BASE}templates/{slot.template.uid}/").status_code == 204
        assert not Slot.objects.filter(pk=slot.pk).exists() and not FixedItem.objects.filter(pk=item.pk).exists()
        assert Slot.all_objects.get(pk=slot.pk).deleted_at is not None

    def test_slot_key_taken_and_rule_validated(self, client):
        slot = SlotFactory(key="panel")
        body = {"template_uid": str(slot.template.uid), "key": "panel", "category_uid": str(slot.category.uid), "qty_rule": {"type": "fixed", "qty": 1}}
        response = client.post(f"{BASE}slots/", body, format="json")
        assert response.status_code == 409 and response.json()["code"] == "slot_key_taken"
        bad = client.post(f"{BASE}slots/", {**body, "key": "other", "qty_rule": {"type": "fixed"}}, format="json")
        assert bad.status_code == 400 and "qty_rule" in bad.json()["errors"]
        bad_key = client.post(f"{BASE}slots/", {**body, "key": "Bad Key"}, format="json")
        assert bad_key.status_code == 400 and "key" in bad_key.json()["errors"]

    def test_slot_refuses_inactive_category_and_deleted_template(self, client):
        slot = SlotFactory()
        inactive = CategoryFactory(is_active=False)
        response = client.patch(f"{BASE}slots/{slot.uid}/", {"category_uid": str(inactive.uid)}, format="json")
        assert response.status_code == 400 and "category" in response.json()["errors"]
        template = TemplateFactory(system_type="UPGRADE")
        template.soft_delete()
        body = {"template_uid": str(template.uid), "key": "x", "category_uid": str(slot.category.uid), "qty_rule": {"type": "fixed", "qty": 1}}
        assert client.post(f"{BASE}slots/", body, format="json").status_code == 400

    def test_fixed_item_needs_a_price_and_a_quantity(self, client):
        template = TemplateFactory()
        base = {"template_uid": str(template.uid), "name": "Item"}
        no_price = client.post(f"{BASE}fixed-items/", {**base, "qty": "1"}, format="json")
        assert no_price.status_code == 400 and "unit_price" in no_price.json()["errors"]
        no_qty = client.post(f"{BASE}fixed-items/", {**base, "unit_price": "1"}, format="json")
        assert no_qty.status_code == 400 and "qty" in no_qty.json()["errors"]
        bad_condition = client.post(f"{BASE}fixed-items/", {**base, "unit_price": "1", "qty": "1", "condition": {"phases": ["2P"]}}, format="json")
        assert bad_condition.status_code == 400 and "condition" in bad_condition.json()["errors"]
        component = ComponentFactory(status=ComponentStatus.ACTIVE)
        linked = client.post(f"{BASE}fixed-items/", {**base, "component_uid": str(component.uid), "qty": "2"}, format="json")
        assert linked.status_code == 201 and linked.json()["component"]["sku"] == component.sku and linked.json()["unit_price"] is None

    def test_retired_component_is_not_selectable(self, client):
        retired = ComponentFactory(status=ComponentStatus.RETIRED)
        template = TemplateFactory()
        response = client.post(f"{BASE}fixed-items/", {"template_uid": str(template.uid), "name": "X", "component_uid": str(retired.uid), "qty": "1"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "component_retired"
        profile = client.post(f"{BASE}package-profiles/", {"key": "p", "label": "P", "structure_material": "GI", "inverter_type": "HYBRID", "battery_component_uid": str(retired.uid)}, format="json")
        assert profile.status_code == 400 and profile.json()["code"] == "component_retired"

    def test_structure_item_pricing_rules(self, client):
        template = StructureTemplateFactory()
        base = {"template_uid": str(template.uid), "name": "Item", "qty_rule": {"type": "kw_interpolated", "points": {"3": 1}}}
        tube = client.post(f"{BASE}structure-items/", {**base, "item_type": "TUBE"}, format="json")
        assert tube.status_code == 400 and "weight_kg" in tube.json()["errors"]
        fixed = client.post(f"{BASE}structure-items/", {**base, "item_type": "FIXED"}, format="json")
        assert fixed.status_code == 400 and "unit_price" in fixed.json()["errors"]
        ok = client.post(f"{BASE}structure-items/", {**base, "item_type": "FIXED", "unit_price": "12"}, format="json")
        assert ok.status_code == 201
        assert StructureTemplateItem.objects.filter(template=template).count() == 1

    @pytest.mark.parametrize(
        "name,factory,field,value,code",
        [
            ("structure-templates", StructureTemplateFactory, "slug", "flat_roof", "structure_template_exists"),
            ("tube-weights", TubeWeightFactory, "tube_size", "2x1", "tube_weight_exists"),
            ("package-profiles", PackageProfileFactory, "key", "ongrid_base", "package_profile_exists"),
        ],
    )
    def test_natural_keys_are_unique(self, client, name, factory, field, value, code):
        factory(**{field: value})
        other = factory()
        response = client.patch(f"{BASE}{name}/{other.uid}/", {field: value}, format="json")
        assert response.status_code == 409 and response.json()["code"] == code

    def test_filters(self, client):
        slot = SlotFactory(key="panel")
        SlotFactory(template=TemplateFactory(system_type="HYBRID"))
        response = client.get(f"{BASE}slots/", {"template": str(slot.template.uid)})
        assert [row["uid"] for row in response.json()["results"]] == [str(slot.uid)]
        assert client.get(f"{BASE}slots/", {"category": slot.category.slug}).json()["count"] == 1
        assert client.get(f"{BASE}templates/", {"system_type": "ONGRID"}).json()["count"] == Template.objects.filter(system_type="ONGRID").count()

    def test_component_usage_blocks_catalog_delete(self, auth_client, make_user):
        component = ComponentFactory(status=ComponentStatus.ACTIVE)
        FixedItemFactory(component=component)
        PackageProfileFactory(battery_component=component)
        catalog = auth_client(make_user(grants={"catalog": "*"}))
        usage = catalog.get(f"/api/v1/catalog/components/{component.uid}/usage/").json()
        names = {section["name"]: section["count"] for section in usage["sections"]} if "sections" in usage else usage
        assert names.get("bom.fixed_items") == 1 and names.get("bom.package_profiles") == 1
        assert catalog.delete(f"/api/v1/catalog/components/{component.uid}/").status_code == 409
