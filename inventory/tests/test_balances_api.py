"""GET inventory/balances/ — the inventory_balance view, filters by component/location, flag gate, permissions, N+1."""

import uuid

import pytest

from catalog.tests.factories import CategoryFactory, ComponentFactory
from inventory.models import Balance, Direction, Reason
from inventory.tests.factories import LocationFactory, MovementFactory, OfficeFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/inventory/balances/"


def out(component, location, qty, reason=Reason.ADJUST):
    extra = {"note": "count"} if reason == Reason.ADJUST else {"ref_type": "projects.project", "ref_uid": uuid.uuid4()}
    return MovementFactory(component=component, location=location, qty=qty, direction=Direction.OUT, reason=reason, **extra)


class TestFlagAndPermissions:
    def test_404_while_the_flag_is_off(self, stock_off, api_client, client):
        assert api_client.get(URL).status_code == 404
        assert client.get(URL).status_code == 404

    def test_anonymous_401_and_missing_grant_403(self, api_client, outsider, viewer):
        assert api_client.get(URL).status_code == 401
        assert outsider.get(URL).status_code == 403
        assert viewer.get(URL).status_code == 200

    def test_read_only(self, client):
        assert client.post(URL, {}, format="json").status_code == 403  # unmapped action: default deny

    def test_scope_is_all(self, auth_client, make_user):
        MovementFactory.create_batch(3)
        assert auth_client(make_user(grants={"inventory": ["view"]})).get(URL).json()["count"] == 3


class TestView:
    def test_signed_sum_per_component_and_location(self, component, store):
        van = LocationFactory(code="VAN")
        MovementFactory(component=component, location=store, qty="10")
        MovementFactory(component=component, location=store, qty="2.5")
        out(component, store, "4", reason=Reason.ISSUE_TO_PROJECT)
        MovementFactory(component=component, location=van, qty="1")
        rows = {row.location.code: row for row in Balance.objects.select_related("location")}
        assert set(rows) == {"HO-STORE", "VAN"}
        assert str(rows["HO-STORE"].qty) == "8.500" and str(rows["HO-STORE"].qty_in) == "12.500" and str(rows["HO-STORE"].qty_out) == "4.000"
        assert rows["HO-STORE"].movement_count == 3 and rows["VAN"].qty == 1

    def test_endpoint_shape_and_filters(self, client, component, store):
        office = OfficeFactory(code="HO", name="Head Office")
        store.office = office
        store.save()
        other = ComponentFactory(sku="INV-0001", category=CategoryFactory(slug="inverter-x"))
        van = LocationFactory(code="VAN")
        MovementFactory(component=component, location=store, qty="5")
        MovementFactory(component=other, location=store, qty="1")
        out(other, store, "1")
        MovementFactory(component=component, location=van, qty="2")
        out(component, van, "3")

        rows = client.get(URL).json()["results"]
        assert len(rows) == 3
        first = client.get(URL, {"component": str(component.uid), "location": str(store.uid)}).json()["results"]
        assert first == [
            {
                "component": {"uid": str(component.uid), "sku": "PNL-0001", "name": "Mono PERC 540 W", "unit": "NOS"},
                "location": {"uid": str(store.uid), "code": "HO-STORE", "name": "Head office store"},
                "office": {"uid": str(office.uid), "code": "HO", "name": "Head Office"},
                "qty": "5.000",
                "qty_in": "5.000",
                "qty_out": "0.000",
                "movement_count": 1,
                "last_movement_at": first[0]["last_movement_at"],
            }
        ]
        assert client.get(URL, {"component": str(component.uid)}).json()["count"] == 2
        assert client.get(URL, {"filter[location]": str(van.uid)}).json()["count"] == 1
        assert client.get(URL, {"office": str(office.uid)}).json()["count"] == 2
        assert client.get(URL, {"category": "inverter-x"}).json()["count"] == 1
        assert [row["qty"] for row in client.get(URL, {"state": "in_stock"}).json()["results"]] == ["5.000"]
        assert client.get(URL, {"state": "zero"}).json()["results"][0]["component"]["sku"] == "INV-0001"
        assert client.get(URL, {"state": "negative"}).json()["results"][0]["qty"] == "-1.000"
        assert client.get(URL, {"search": "VAN"}).json()["count"] == 1
        assert client.get(URL, {"ordering": "qty"}).json()["results"][0]["qty"] == "-1.000"
        assert client.get(URL, {"state": "bogus"}).status_code == 400

    def test_deleted_locations_are_left_out_and_no_n_plus_one(self, client, django_assert_max_num_queries):
        for _ in range(6):
            MovementFactory(location=LocationFactory(office=OfficeFactory()))
        gone = LocationFactory()
        component = ComponentFactory()
        MovementFactory(component=component, location=gone, qty="1")
        out(component, gone, "1")
        gone.soft_delete()
        with django_assert_max_num_queries(8):
            rows = client.get(URL).json()["results"]
        assert len(rows) == 6 and all(row["office"] for row in rows)
