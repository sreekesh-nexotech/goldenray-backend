"""inventory/locations/ — flag gate, permissions, CRUD, code uniqueness, HR office link, delete guard, N+1."""

import pytest

from audit.models import AuditLog
from inventory.models import Direction, Location, Reason
from inventory.tests.factories import LocationFactory, MovementFactory, OfficeFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/inventory/locations/"


def detail(location, suffix=""):
    return f"{URL}{location.uid}/{suffix}"


class TestFlagAndPermissions:
    def test_every_route_is_404_while_the_flag_is_off(self, stock_off, api_client, client, store):
        for response in (
            api_client.get(URL),
            client.get(URL),
            client.get(detail(store)),
            client.post(URL, {"code": "X", "name": "X"}, format="json"),
            client.patch(detail(store), {"name": "x"}, format="json"),
            client.delete(detail(store)),
        ):
            assert response.status_code == 404 and response.json()["code"] == "not_found"
        assert Location.objects.get(pk=store.pk).name == "Head office store"

    def test_anonymous_is_401(self, api_client, store):
        assert api_client.get(URL).status_code == 401
        assert api_client.get(detail(store)).status_code == 401
        assert api_client.post(URL, {"code": "X", "name": "X"}, format="json").status_code == 401

    def test_view_grant_reads_and_edit_grant_writes(self, viewer, outsider, store):
        assert viewer.get(URL).status_code == 200
        assert viewer.get(detail(store)).status_code == 200
        assert viewer.post(URL, {"code": "X", "name": "X"}, format="json").status_code == 403
        assert viewer.patch(detail(store), {"name": "x"}, format="json").status_code == 403
        assert viewer.delete(detail(store)).status_code == 403
        assert outsider.get(URL).status_code == 403

    def test_scope_is_all(self, auth_client, make_user):
        LocationFactory.create_batch(3)
        assert auth_client(make_user(grants={"inventory": ["view"]})).get(URL).json()["count"] == 3


class TestCreateAndList:
    def test_create_with_an_office_and_audit(self, client, keeper):
        office = OfficeFactory(code="HO", name="Head Office")
        response = client.post(URL, {"code": "HO-STORE", "name": "Main store", "office": str(office.uid)}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["code"] == "HO-STORE" and body["version"] == 1
        assert body["office"] == {"uid": str(office.uid), "code": "HO", "name": "Head Office"}
        entry = AuditLog.objects.get(action="inventory.location_created")
        assert entry.actor == keeper and entry.after["office"] == str(office.uid)

    def test_create_without_office(self, client):
        response = client.post(URL, {"code": "VAN-1", "name": "Service van"}, format="json")
        assert response.status_code == 201 and response.json()["office"] is None

    def test_validation_envelope(self, client):
        response = client.post(URL, {"code": "", "name": ""}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"
        assert {"code", "name"} <= set(response.json()["errors"])

    @pytest.mark.parametrize("code", ["-X", "has space", "a/b", "x" * 31])
    def test_code_format(self, client, code):
        response = client.post(URL, {"code": code, "name": "Store"}, format="json")
        assert response.status_code == 400 and "code" in response.json()["errors"]

    def test_unknown_or_deleted_office_is_refused(self, client):
        office = OfficeFactory()
        office.soft_delete()
        for value in (str(office.uid), "7c1d3b0e-0000-4000-8000-000000000000"):
            response = client.post(URL, {"code": "S1", "name": "Store", "office": value}, format="json")
            assert response.status_code == 400 and response.json()["errors"] == {"office": ["Unknown office."]}
        assert client.post(URL, {"code": "S1", "name": "Store", "office": "not-a-uuid"}, format="json").status_code == 400

    def test_codes_are_unique_case_insensitive_among_live_rows(self, client, store):
        clash = client.post(URL, {"code": "ho-store", "name": "Other"}, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "location_code_taken"
        store.soft_delete()
        assert client.post(URL, {"code": "ho-store", "name": "Other"}, format="json").status_code == 201

    def test_list_filters_search_and_no_n_plus_one(self, client, django_assert_max_num_queries):
        office = OfficeFactory()
        for index in range(6):
            LocationFactory(office=office if index % 2 else None)
        with django_assert_max_num_queries(8):
            rows = client.get(URL).json()["results"]
        assert len(rows) == 6
        assert client.get(URL, {"office": str(office.uid)}).json()["count"] == 3
        assert client.get(URL, {"filter[has_office]": "false"}).json()["count"] == 3
        assert client.get(URL, {"search": rows[0]["code"]}).json()["count"] == 1
        assert [row["code"] for row in client.get(URL, {"ordering": "-code"}).json()["results"]] == sorted((row["code"] for row in rows), reverse=True)

    def test_a_soft_deleted_office_reads_as_no_office(self, client):
        office = OfficeFactory()
        location = LocationFactory(office=office)
        office.soft_delete()
        assert client.get(detail(location)).json()["office"] is None
        assert client.get(URL, {"office": str(office.uid)}).json()["count"] == 0
        assert client.get(URL, {"has_office": "true"}).json()["count"] == 0


class TestUpdateAndDelete:
    def test_update_with_expected_version(self, client, store):
        office = OfficeFactory()
        response = client.patch(detail(store), {"name": "Store 1", "office": str(office.uid), "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["version"] == 2
        entry = AuditLog.objects.get(action="inventory.location_updated")
        assert entry.before == {"name": "Head office store", "office": None} and entry.after == {"name": "Store 1", "office": str(office.uid)}

    def test_unlink_the_office(self, client):
        location = LocationFactory(office=OfficeFactory())
        response = client.patch(detail(location), {"office": None}, format="json")
        assert response.status_code == 200 and response.json()["office"] is None

    def test_stale_version(self, client):
        location = LocationFactory(version=3)
        response = client.patch(detail(location), {"name": "x", "expected_version": 2}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert client.delete(f"{detail(location)}?expected_version=2").json()["code"] == "stale_version"

    def test_noop_update_keeps_the_version(self, client, store):
        assert client.patch(detail(store), {"name": "Head office store"}, format="json").json()["version"] == 1
        assert not AuditLog.objects.filter(action="inventory.location_updated").exists()

    def test_code_clash_on_update(self, client, store):
        other = LocationFactory(code="VAN")
        response = client.patch(detail(other), {"code": "ho-store"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "location_code_taken"

    def test_blank_name_on_update(self, client, store):
        response = client.patch(detail(store), {"name": "  "}, format="json")
        assert response.status_code == 400 and "name" in response.json()["errors"]

    def test_delete_is_refused_while_the_location_holds_stock(self, client, store, component):
        MovementFactory(component=component, location=store, qty="5")
        response = client.delete(detail(store))
        assert response.status_code == 409 and response.json()["code"] == "location_has_stock"
        assert Location.objects.filter(pk=store.pk).exists()

    def test_delete_once_the_stock_is_zero(self, client, keeper, store, component):
        MovementFactory(component=component, location=store, qty="5")
        MovementFactory(component=component, location=store, qty="5", direction=Direction.OUT, reason=Reason.ISSUE_TO_PROJECT, ref_type="projects.project", ref_uid=store.uid)
        assert client.delete(detail(store)).status_code == 204
        assert not Location.objects.filter(pk=store.pk).exists() and Location.all_objects.get(pk=store.pk).deleted_at
        assert AuditLog.objects.get(action="inventory.location_deleted").actor == keeper
        assert client.get(detail(store)).status_code == 404


class TestReceivingLocation:
    """INVENTORY_RECEIVING_LOCATION names a location by code: renaming or deleting that location from the Studio would
    make every procurement.batch_committed receipt fail and park, so both are refused until the setting changes."""

    @pytest.fixture(autouse=True)
    def receiving(self, settings):
        settings.INVENTORY_RECEIVING_LOCATION = "ho-store"

    def test_its_code_cannot_change(self, client, store):
        response = client.patch(detail(store), {"code": "HQ-STORE"}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "receiving_location"
        assert "code" in response.json()["errors"] and Location.objects.get(pk=store.pk).code == "HO-STORE"
        assert client.patch(detail(store), {"name": "Renamed", "code": "HO-STORE"}, format="json").status_code == 200
        assert client.patch(detail(store), {"code": "Ho-Store"}, format="json").json()["code"] == "Ho-Store"  # still matches

    def test_it_cannot_be_deleted(self, client, store):
        response = client.delete(detail(store))
        assert response.status_code == 409 and response.json()["code"] == "receiving_location"
        assert Location.objects.filter(pk=store.pk).exists()

    def test_other_locations_and_receiving_off_are_unaffected(self, client, settings, store):
        other = LocationFactory(code="VAN")
        assert client.patch(detail(other), {"code": "VAN-2"}, format="json").status_code == 200
        assert client.delete(detail(other)).status_code == 204
        settings.INVENTORY_RECEIVING_LOCATION = ""
        assert client.patch(detail(store), {"code": "HQ-STORE"}, format="json").status_code == 200
        assert client.delete(detail(store)).status_code == 204
