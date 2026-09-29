"""POST /api/v1/bom/build/ — dry BOM through engines.bom_builder (nothing written)."""

from decimal import Decimal

import pytest

from audit.models import AuditLog
from bom.tests.factories import PackageProfileFactory, SlotFactory, TemplateFactory
from catalog.tests.factories import CategoryFactory, ComponentFactory, ComponentTierFactory, PanelSpecFactory
from pricing.tests.factories import PriceFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/bom/build/"


def test_permissions(api_client, outsider, viewer, world):
    body = {"system_type": "ONGRID", "size": "3", "tier": "BASE"}
    assert api_client.post(URL, body, format="json").status_code == 401
    assert outsider.post(URL, body, format="json").status_code == 403
    assert viewer.post(URL, body, format="json").status_code == 200  # a dry build is a read (bom.view)


def test_dry_build_with_structure(client, world):
    audits = AuditLog.objects.count()
    response = client.post(URL, {"system_type": "ONGRID", "size": "3", "tier": "BASE", "structure": "elevated"}, format="json")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert [line["name"] for line in body["lines"]] == ["Panel 540", "Inverter 3", "Cable 4sqmm", "MC4"]
    panel, inverter, cable, mc4 = body["lines"]
    assert panel["qty"] == 6 and panel["unitPrice"] == 13122 and panel["gst"] == 5 and inverter["selectionMethod"] == "NEAREST_KW_TO_SYSTEM_SIZE"
    assert mc4["category"] == "fixed" and mc4["qty"] == 3 and mc4["unitPrice"] == 56
    assert body["system_config"]["phase"] == "1P" and body["warnings"] == []
    assert body["structure"]["slug"] == "elevated" and body["structure"]["tube_kg"] == 76.5 and len(body["structure"]["lines"]) == 2
    assert AuditLog.objects.count() == audits  # nothing written


def test_three_phase_size_condition_and_unfilled_required_slot(client, world):
    body = client.post(URL, {"system_type": "ONGRID", "size": "5tp", "tier": "VALUE"}, format="json").json()
    names = [line["name"] for line in body["lines"]]
    assert "Inverter 5" in names and "3P only" in names and body["system_config"]["phase"] == "3P"
    assert body["warnings"] == [{"code": "slot_unfilled", "slot": "dc_cable", "message": "No eligible component with a price for slot dc_cable (dc_cable)."}]
    assert body["structure"] is None


def test_panel_count_from_wattage_profile_and_selection(client, world):
    panels = world.slots.get(key="panel").category
    big = ComponentFactory(category=panels, name="Panel 600", sku="p600")
    ComponentTierFactory(component=big, tier="BASE")
    PanelSpecFactory(component=big, wattage_w=600)
    PriceFactory(component=big, amount=Decimal("15000.00"))
    PackageProfileFactory(key="ongrid_base", label="On-Grid Base", structure_material="GP")
    body = client.post(URL, {"system_type": "ONGRID", "size": "3", "tier": "BASE", "selections": {"panel": "p600"}}, format="json").json()
    panel = body["lines"][0]
    assert panel["name"] == "Panel 600" and panel["qty"] == 5 and panel["selectionMethod"] == "SALES_SELECTION"
    assert body["profile"]["packageKey"] == "ongrid_base"


@pytest.mark.parametrize(
    "body,code",
    [
        ({"system_type": "ONGRID", "size": "7", "tier": "BASE"}, "bom_build_refused"),
        ({"system_type": "ONGRID", "size": "3", "tier": "BASE", "phase": "3P"}, "unsupported_configuration"),
        ({"system_type": "HYBRID", "size": "3", "tier": "BASE"}, "bom_template_missing"),
        ({"system_type": "ONGRID", "size": "3", "tier": "BASE", "structure": "ground"}, "validation_error"),
        ({"system_type": "UPGRADE", "size": "3", "tier": "BASE"}, "validation_error"),
        ({"system_type": "ONGRID", "size": "3", "tier": "BASE", "selections": {"panel": "nope"}}, "bom_build_refused"),
    ],
)
def test_refusals(client, world, body, code):
    response = client.post(URL, body, format="json")
    assert response.status_code == 400 and response.json()["code"] == code, response.json()


def test_other_rule_types_become_size_tables(client, world):
    meters = CategoryFactory(slug="meter")
    meter = ComponentFactory(category=meters, name="Meter")
    ComponentTierFactory(component=meter, tier="BASE")
    PriceFactory(component=meter, amount=Decimal("1400.00"))
    SlotFactory(template=TemplateFactory(system_type="ONGRID"), key="meter", category=meters, sort_order=4, qty_rule={"type": "by_phase", "1P": 1, "3P": 2})
    body = client.post(URL, {"system_type": "ONGRID", "size": "3", "tier": "BASE"}, format="json").json()
    assert next(line for line in body["lines"] if line["name"] == "Meter")["qty"] == 1
