"""catalog/battery-families/ — CRUD and the in-use guard (battery specs and inverter compatibility lists)."""

import pytest

from audit.models import AuditLog
from catalog.models import BatteryFamily
from catalog.tests.factories import BatteryFamilyFactory, battery, inverter

pytestmark = pytest.mark.django_db
URL = "/api/v1/catalog/battery-families/"


def detail(family):
    return f"{URL}{family.uid}/"


def test_permissions(api_client, outsider, viewer):
    family = BatteryFamilyFactory()
    assert api_client.get(URL).status_code == 401
    assert outsider.get(URL).status_code == 403
    assert viewer.get(detail(family)).status_code == 200
    assert viewer.post(URL, {"slug": "x", "name": "x", "voltage_class": "HV"}, format="json").status_code == 403
    assert viewer.patch(detail(family), {"name": "y"}, format="json").status_code == 403
    assert viewer.delete(detail(family)).status_code == 403


def test_crud(client):
    response = client.post(URL, {"slug": "seg-lv", "name": "SEG LV", "voltage_class": "48V", "notes": "Pylontech-compatible"}, format="json")
    assert response.status_code == 201 and response.json()["voltage_class"] == "48V"
    family = BatteryFamily.objects.get()
    assert client.patch(detail(family), {"name": "SEG 48 V", "expected_version": 1}, format="json").json()["version"] == 2
    assert client.patch(detail(family), {"name": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"
    assert client.post(URL, {"slug": "seg-lv", "name": "dup", "voltage_class": "HV"}, format="json").json()["code"] == "battery_family_slug_taken"
    assert client.post(URL, {"slug": "x", "name": "x", "voltage_class": "12V"}, format="json").json()["errors"]["voltage_class"]
    assert client.delete(detail(family)).status_code == 204 and not BatteryFamily.objects.exists()
    actions = list(AuditLog.objects.filter(object_uid=family.uid).order_by("id").values_list("action", flat=True))
    assert actions == ["catalog.battery_family_created", "catalog.battery_family_updated", "catalog.battery_family_deleted"]
    assert BatteryFamily.all_objects.get(pk=family.pk).deleted_at is not None  # soft delete


def test_in_use_guards(client):
    family = BatteryFamilyFactory(slug="seg-lv")
    component = battery(spec={"family": family})
    assert client.delete(detail(family)).json()["code"] == "battery_family_in_use"
    assert client.patch(detail(family), {"slug": "seg"}, format="json").json()["code"] == "battery_family_in_use"
    component.soft_delete()
    other = BatteryFamilyFactory(slug="hv-stack")
    inverter(spec={"compatible_battery_families": ["hv-stack"]})
    assert client.delete(detail(other)).json()["code"] == "battery_family_in_use"
    assert client.patch(detail(family), {"slug": "seg"}, format="json").json()["slug"] == "seg"
    assert client.delete(detail(family)).status_code == 204


def test_list_query_budget(client, django_assert_max_num_queries):
    BatteryFamilyFactory.create_batch(20)
    with django_assert_max_num_queries(10):
        assert client.get(URL).json()["count"] == 20
