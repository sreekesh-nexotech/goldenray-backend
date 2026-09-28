"""reference/* staff CRUD (module reference_data) — every list: auth, grants, scope, validation, conflicts, versions."""

import pytest

from audit.models import AuditLog
from reference.tests import factories

pytestmark = pytest.mark.django_db

# key → (entity, factory, create payload, factory kwargs clashing with the payload, (invalid payload, error field))
LISTS = {
    "tariffs": (
        "kseb_tariff",
        factories.KsebTariffFactory,
        {"slab_from_units": 0, "slab_to_units": 300, "rate_per_unit": "6.75", "phase": "1P", "effective_from": "2026-04-01"},
        {"slab_from_units": 0, "phase": "1P", "effective_from": "2026-04-01"},
        ({"slab_from_units": 500, "slab_to_units": 100, "rate_per_unit": "7"}, "slab_to_units"),
    ),
    "device-types": ("device_type", factories.DeviceTypeFactory, {"name": "Iron", "watts": 1000, "k_value": 1, "url": "https://cdn.example.com/iron.svg"}, {"name": "IRON"}, ({"name": ""}, "name")),
    "wattages": ("wattage", factories.WattageFactory, {"value": 540}, {"value": 540}, ({"value": 0}, "value")),
    "room-sizes": ("room_size", factories.RoomSizeFactory, {"bhk_type": 3, "size": 1800, "units": 250}, {"bhk_type": 3, "size": 1800}, ({"bhk_type": 0, "size": 100, "units": 1}, "bhk_type")),
    "ev-cars": (
        "ev_car",
        factories.EvCarFactory,
        {"model": "Tata Nexon EV", "battery_capacity": 30.2, "claimed_range": 312, "adjusted_real_world_range": 212, "ex_showroom_price": "1249000"},
        {"model": "tata nexon ev"},
        ({"model": "X", "battery_capacity": -1, "claimed_range": 1, "adjusted_real_world_range": 1, "ex_showroom_price": "1"}, "battery_capacity"),
    ),
    "ev-scooters": (
        "ev_scooter",
        factories.EvScooterFactory,
        {"model": "Ather 450X", "battery_capacity": 3.7, "claimed_range": 100, "adjusted_real_world_range": 80, "ex_showroom_price": "135000", "energy_consumption": 0.028},
        {"model": "Ather 450X"},
        ({"model": "Y", "battery_capacity": 1, "claimed_range": 1, "adjusted_real_world_range": 1, "ex_showroom_price": "-5"}, "ex_showroom_price"),
    ),
    "appliances": (
        "appliance",
        factories.ApplianceFactory,
        {"code": "water_pump", "name": "Water Pump", "name_ml": "വാട്ടർ പമ്പ്", "icon": "🚰", "watts": 750, "default_hours": "1.00", "is_optional": True},
        {"code": "water_pump"},
        ({"code": "Water Pump", "name": "x", "watts": 1, "default_hours": "25"}, "default_hours"),
    ),
}
KEYS = list(LISTS)


def url(key, row=None):
    return f"/api/v1/reference/{key}/" + (f"{row.uid}/" if row is not None else "")


@pytest.fixture
def editor(auth_client, make_user):
    return auth_client(make_user(grants={"reference_data": "*"}))


@pytest.mark.parametrize("key", KEYS)
class TestEveryList:
    def test_anonymous_is_401(self, api_client, key):
        row = LISTS[key][1]()
        assert api_client.get(url(key)).status_code == 401
        assert api_client.post(url(key), {}, format="json").status_code == 401
        assert api_client.delete(url(key, row)).status_code == 401

    def test_grants(self, auth_client, make_user, key):
        row = LISTS[key][1]()
        viewer = auth_client(make_user(grants={"reference_data": ["view"]}))
        assert viewer.get(url(key)).status_code == 200 and viewer.get(url(key, row)).status_code == 200
        assert viewer.post(url(key), LISTS[key][2], format="json").status_code == 403
        assert viewer.patch(url(key, row), {"sort_order": 3}, format="json").status_code == 403
        assert viewer.delete(url(key, row)).status_code == 403
        assert auth_client(make_user(grants={"reference_data": ["view", "edit"]})).delete(url(key, row)).status_code == 403
        assert auth_client(make_user(grants={"catalog": "*"})).get(url(key)).status_code == 403

    def test_scope_is_all(self, auth_client, make_user, key):
        LISTS[key][1].create_batch(2)
        assert auth_client(make_user(grants={"reference_data": ["view"]})).get(url(key)).json()["count"] == 2

    def test_create_goes_to_the_end_and_is_audited(self, editor, key):
        entity, factory, payload, _clash, _invalid = LISTS[key]
        factory(sort_order=7)
        response = editor.post(url(key), payload, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["version"] == 1 and body["is_active"] is True and body["sort_order"] == 8
        assert AuditLog.objects.filter(action=f"reference.{entity}_created").count() == 1

    def test_validation_envelope(self, editor, key):
        payload, field = LISTS[key][4]
        response = editor.post(url(key), payload, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"], response.json()

    def test_live_uniqueness(self, editor, key):
        entity, factory, payload, clash, _invalid = LISTS[key]
        existing = factory(**clash)
        response = editor.post(url(key), payload, format="json")
        assert response.status_code == 409 and response.json()["code"] == f"{entity}_exists"
        existing.soft_delete()
        assert editor.post(url(key), payload, format="json").status_code == 201

    def test_update_stale_version_and_delete(self, editor, key):
        entity, factory, *_ = LISTS[key]
        row = factory()
        response = editor.patch(url(key, row), {"is_active": False, "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["is_active"] is False and response.json()["version"] == 2
        assert AuditLog.objects.get(action=f"reference.{entity}_updated").after == {"is_active": False}
        stale = editor.patch(url(key, row), {"sort_order": 4, "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        assert editor.delete(f"{url(key, row)}?expected_version=1").status_code == 409
        assert editor.delete(url(key, row)).status_code == 204
        assert editor.get(url(key, row)).status_code == 404
        assert type(row).all_objects.get(pk=row.pk).deleted_at is not None

    def test_list_without_n_plus_one(self, editor, key, django_assert_max_num_queries):
        LISTS[key][1].create_batch(15)
        with django_assert_max_num_queries(8):
            body = editor.get(url(key), {"page_size": 200}).json()
        assert body["count"] == 15
        assert editor.get(url(key), {"is_active": "false"}).json()["count"] == 0


class TestTariffRules:
    def test_update_cannot_invert_the_slab(self, editor):
        tariff = factories.KsebTariffFactory(slab_from_units=100, slab_to_units=200)
        response = editor.patch(url("tariffs", tariff), {"slab_to_units": 50}, format="json")
        assert response.status_code == 400 and "slab_to_units" in response.json()["errors"]

    def test_blank_phase_is_refused(self, editor):
        response = editor.post(url("tariffs"), {"slab_from_units": 0, "rate_per_unit": "1", "phase": ""}, format="json")
        assert response.status_code == 400 and "phase" in response.json()["errors"]


class TestPincodes:
    URL = "/api/v1/reference/pincodes/"

    def test_create_with_offices_derives_district(self, editor):
        payload = {"pincode": "688011", "offices": [{"office_name": "Kuppappuram BO", "district": "ALAPPUZHA", "state": "KERALA", "region": "Kochi Region", "division": "Alleppey Division"}]}
        response = editor.post(self.URL, payload, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["district"] == "ALAPPUZHA" and body["state"] == "KERALA" and [office["office_name"] for office in body["offices"]] == ["Kuppappuram BO"]
        assert AuditLog.objects.get(action="reference.pincode_created").after["offices"] == ["Kuppappuram BO"]

    def test_office_list_is_replaced(self, editor):
        office = factories.PincodeOfficeFactory(office_name="Old BO")
        keep = factories.PincodeOfficeFactory(pincode=office.pincode, office_name="Keep BO")
        pincode = office.pincode
        payload = {"offices": [{"uid": str(keep.uid), "office_name": "Kept BO"}, {"office_name": "New BO", "district": "KOTTAYAM"}], "expected_version": 1}
        response = editor.patch(f"{self.URL}{pincode.uid}/", payload, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert [row["office_name"] for row in body["offices"]] == ["Kept BO", "New BO"] and body["version"] == 2
        assert AuditLog.objects.get(action="reference.pincode_updated").after == {"offices": ["Kept BO", "New BO"]}
        foreign = factories.PincodeOfficeFactory()
        bad = editor.patch(f"{self.URL}{pincode.uid}/", {"offices": [{"uid": str(foreign.uid), "office_name": "x"}]}, format="json")
        assert bad.status_code == 400 and "offices" in bad.json()["errors"]

    def test_validation_conflict_stale_delete(self, editor):
        assert editor.post(self.URL, {"pincode": "12345"}, format="json").status_code == 400
        existing = factories.PincodeFactory(pincode="688011")
        clash = editor.post(self.URL, {"pincode": "688011"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "pincode_exists"
        assert editor.patch(f"{self.URL}{existing.uid}/", {"serviceable": False, "expected_version": 9}, format="json").status_code == 409
        assert editor.delete(f"{self.URL}{existing.uid}/").status_code == 204

    def test_search_filter_and_no_n_plus_one(self, editor, django_assert_max_num_queries):
        for _ in range(10):
            factories.PincodeOfficeFactory.create_batch(2, pincode=factories.PincodeFactory())
        factories.PincodeOfficeFactory(office_name="Kuppappuram BO", pincode=factories.PincodeFactory(pincode="688011", serviceable=False))
        with django_assert_max_num_queries(8):
            assert editor.get(self.URL, {"page_size": 200}).json()["count"] == 11
        assert [row["pincode"] for row in editor.get(self.URL, {"search": "kuppap"}).json()["results"]] == ["688011"]
        assert editor.get(self.URL, {"serviceable": "false"}).json()["count"] == 1

    def test_permissions(self, auth_client, make_user, api_client):
        assert api_client.get(self.URL).status_code == 401
        viewer = auth_client(make_user(grants={"reference_data": ["view"]}))
        assert viewer.get(self.URL).status_code == 200 and viewer.post(self.URL, {"pincode": "688011"}, format="json").status_code == 403
