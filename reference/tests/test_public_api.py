"""/api/public/v1/reference/* — shape, active rows only, order, pagination, long cache + invalidation, throttle scope."""

import datetime as dt

import pytest
from freezegun import freeze_time

from reference.tests import factories

pytestmark = pytest.mark.django_db
BASE = "/api/public/v1/reference/"
PUBLIC = {
    "device-types": (factories.DeviceTypeFactory, {"uid", "name", "show_in_ui", "url", "watts"}),
    "wattages": (factories.WattageFactory, {"uid", "value", "show_in_ui"}),
    "room-sizes": (factories.RoomSizeFactory, {"uid", "bhk_type", "size", "units"}),
    "ev-cars": (factories.EvCarFactory, {"uid", "model", "battery_capacity", "claimed_range", "adjusted_real_world_range", "ex_showroom_price", "energy_consumption"}),
    "ev-scooters": (factories.EvScooterFactory, {"uid", "model", "battery_capacity", "claimed_range", "adjusted_real_world_range", "ex_showroom_price", "energy_consumption"}),
    "appliances": (factories.ApplianceFactory, {"uid", "code", "name", "name_ml", "icon", "watts", "default_hours", "is_optional"}),
    "tariffs": (factories.KsebTariffFactory, {"uid", "slab_from_units", "slab_to_units", "phase", "rate_per_unit", "fixed_charge", "effective_from"}),
}


@pytest.mark.parametrize("key", list(PUBLIC))
class TestLists:
    def test_shape_order_and_active_rows_only(self, api_client, key, django_assert_max_num_queries):
        factory, fields = PUBLIC[key]
        # tariffs are a slab schedule: ordered by slab_from_units (the others by sort_order)
        slabs = ({"slab_from_units": 500}, {"slab_from_units": 0}) if key == "tariffs" else ({}, {})
        second = factory(sort_order=2, **slabs[0])
        first = factory(sort_order=1, **slabs[1])
        factory(sort_order=3, is_active=False)
        factory(sort_order=4).soft_delete()
        with django_assert_max_num_queries(4):
            response = api_client.get(f"{BASE}{key}/")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"results", "count", "next", "previous"} and body["count"] == 2
        assert [row["uid"] for row in body["results"]] == [str(first.uid), str(second.uid)]
        assert set(body["results"][0]) == fields and "id" not in body["results"][0]

    def test_page_size_up_to_200(self, api_client, key):
        factory, _ = PUBLIC[key]
        factory.create_batch(30)
        assert len(api_client.get(f"{BASE}{key}/").json()["results"]) == 25
        body = api_client.get(f"{BASE}{key}/", {"page_size": 200}).json()
        assert len(body["results"]) == 30 and body["next"] is None
        assert len(api_client.get(f"{BASE}{key}/", {"page_size": 1000}).json()["results"]) == 30  # capped at 200

    def test_cache_headers_and_invalidation(self, api_client, auth_client, make_user, key):
        factory, _ = PUBLIC[key]
        row = factory()
        first = api_client.get(f"{BASE}{key}/")
        assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS" and first["ETag"]
        assert api_client.get(f"{BASE}{key}/")["X-Cache"] == "HIT"
        assert api_client.get(f"{BASE}{key}/", HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304
        auth_client(make_user(grants={"reference_data": "*"})).patch(f"/api/v1/reference/{key}/{row.uid}/", {"is_active": False}, format="json")
        after = api_client.get(f"{BASE}{key}/")
        assert after["X-Cache"] == "MISS" and after.json()["count"] == 0

    def test_anonymous_public_read(self, key):
        from reference.views.public import PUBLIC_LIST_VIEWS

        view = PUBLIC_LIST_VIEWS[key]()
        assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "GET"})()) == "public_read"
        assert view.cache_ttl == 24 * 60 * 60


def test_legacy_list_order_follows_sort_order_then_natural_key(api_client):
    factories.WattageFactory(value=500, sort_order=0)
    factories.WattageFactory(value=40, sort_order=0)
    assert [row["value"] for row in api_client.get(f"{BASE}wattages/").json()["results"]] == [40, 500]


def test_writes_are_refused(api_client):
    assert api_client.post(f"{BASE}wattages/", {"value": 1}, format="json").status_code == 405


class TestTariffs:
    def test_only_the_schedule_in_force_today(self, api_client):
        factories.KsebTariffFactory(slab_from_units=0, slab_to_units=300, effective_from=None)
        new = factories.KsebTariffFactory(slab_from_units=0, slab_to_units=250, effective_from=dt.date(2026, 4, 1))
        factories.KsebTariffFactory(slab_from_units=0, slab_to_units=200, effective_from=dt.date(2027, 4, 1))
        three_phase = factories.KsebTariffFactory(slab_from_units=0, slab_to_units=None, phase="3P")
        with freeze_time("2026-09-28"):
            rows = api_client.get(f"{BASE}tariffs/").json()["results"]
        assert [row["uid"] for row in rows] == [str(new.uid), str(three_phase.uid)]
        assert rows[0]["rate_per_unit"] == "6.7500" and rows[0]["phase"] is None and rows[1]["slab_to_units"] is None


class TestPincodeLookup:
    def test_lookup_returns_offices_in_legacy_order(self, api_client, django_assert_max_num_queries):
        pincode = factories.PincodeFactory(pincode="686102", district="KOTTAYAM")
        factories.PincodeOfficeFactory(pincode=pincode, office_name="B BO", district="ALAPPUZHA", sort_order=20)
        factories.PincodeOfficeFactory(pincode=pincode, office_name="A BO", district="KOTTAYAM", sort_order=10)
        factories.PincodeOfficeFactory(pincode=pincode, office_name="Gone BO", sort_order=5).soft_delete()
        with django_assert_max_num_queries(4):
            response = api_client.get(f"{BASE}pincodes/686102/")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"pincode", "district", "state", "serviceable", "distance_km_from_office", "offices"}
        assert body["district"] == "KOTTAYAM" and [office["office_name"] for office in body["offices"]] == ["A BO", "B BO"]
        assert set(body["offices"][0]) == {"office_name", "district", "state", "region", "division"}
        assert response["Cache-Control"] == "public, max-age=60"

    @pytest.mark.parametrize("code", ["999999", "12345", "abcdef"])
    def test_unknown_or_malformed_is_404(self, api_client, code):
        factories.PincodeFactory(pincode="686103", is_active=False)
        response = api_client.get(f"{BASE}pincodes/{code}/")
        assert response.status_code == 404 and response.json()["code"] == "not_found"
        assert api_client.get(f"{BASE}pincodes/686103/").status_code == 404

    def test_pincodes_are_never_listed(self, api_client):
        factories.PincodeFactory.create_batch(3)
        assert api_client.get(f"{BASE}pincodes/").status_code == 404

    def test_staff_edit_invalidates_the_lookup(self, api_client, auth_client, make_user):
        pincode = factories.PincodeFactory(pincode="688011")
        assert api_client.get(f"{BASE}pincodes/688011/").json()["serviceable"] is True
        auth_client(make_user(grants={"reference_data": "*"})).patch(f"/api/v1/reference/pincodes/{pincode.uid}/", {"serviceable": False}, format="json")
        assert api_client.get(f"{BASE}pincodes/688011/").json()["serviceable"] is False
