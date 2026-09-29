"""inventory/movements/ — flag gate, permissions, append-only API, validation, the negative-stock rule, filters, N+1."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.tests.factories import UserFactory
from audit.models import AuditLog
from catalog.tests.factories import CategoryFactory, ComponentFactory
from inventory.models import Movement
from inventory.tests.conftest import events
from inventory.tests.factories import LocationFactory, MovementFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/inventory/movements/"


def body(component, location, **overrides):
    return {"component": str(component.uid), "location": str(location.uid), "qty": "10", "direction": "IN", "reason": "PURCHASE", **overrides}


class TestFlagAndPermissions:
    def test_404_while_the_flag_is_off(self, stock_off, api_client, client, component, store):
        assert api_client.get(URL).status_code == 404
        assert client.get(URL).status_code == 404
        response = client.post(URL, body(component, store), format="json")
        assert response.status_code == 404 and response.json()["code"] == "not_found"
        assert not Movement.objects.exists()

    def test_anonymous_is_401(self, api_client, component, store):
        assert api_client.get(URL).status_code == 401
        assert api_client.post(URL, body(component, store), format="json").status_code == 401

    def test_view_grant_lists_edit_grant_posts(self, viewer, outsider, component, store):
        assert viewer.get(URL).status_code == 200
        assert viewer.post(URL, body(component, store), format="json").status_code == 403
        assert outsider.get(URL).status_code == 403
        assert not Movement.objects.exists()

    def test_scope_is_all(self, auth_client, make_user):
        MovementFactory.create_batch(3)
        assert auth_client(make_user(grants={"inventory": ["view"]})).get(URL).json()["count"] == 3

    def test_movements_are_never_edited_or_deleted(self, client):
        movement = MovementFactory()
        assert client.get(f"{URL}{movement.uid}/").status_code == 404
        assert client.patch(f"{URL}{movement.uid}/", {"qty": "1"}, format="json").status_code == 404
        assert client.delete(f"{URL}{movement.uid}/").status_code == 404
        assert client.put(URL, {}, format="json").status_code == 403  # unmapped action: default deny
        assert client.delete(URL).status_code == 403
        assert Movement.objects.get(pk=movement.pk).qty == movement.qty


class TestRecord:
    def test_purchase_in(self, client, keeper, component, store):
        response = client.post(URL, body(component, store, qty="12.5", note="opening stock"), format="json")
        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["qty"] == "12.500" and data["direction"] == "IN" and data["reason"] == "PURCHASE"
        assert data["component"] == {"uid": str(component.uid), "sku": "PNL-0001", "name": "Mono PERC 540 W", "unit": "NOS"}
        assert data["location"]["code"] == "HO-STORE" and data["by"]["uid"] == str(keeper.uid)
        assert data["balance_after"] == "12.500" and data["negative_override"] is False
        movement = Movement.objects.get(uid=data["uid"])
        assert movement.created_by == keeper and movement.by == keeper
        entry = AuditLog.objects.get(action="inventory.movement_recorded")
        assert entry.actor == keeper and entry.after["balance_after"] == "12.500" and entry.after["qty"] == "12.500"
        [payload] = events("inventory.movement_recorded")
        assert payload["movement_uid"] == data["uid"] and payload["balance_after"] == "12.500"

    def test_issue_within_stock(self, client, component, store):
        MovementFactory(component=component, location=store, qty="10")
        project = str(uuid.uuid4())
        response = client.post(URL, body(component, store, qty="4", direction="OUT", reason="ISSUE_TO_PROJECT", ref_type="projects.project", ref_uid=project), format="json")
        assert response.status_code == 201, response.json()
        assert response.json()["balance_after"] == "6.000" and response.json()["ref_uid"] == project

    def test_out_below_zero_is_409(self, client, component, store):
        MovementFactory(component=component, location=store, qty="3")
        response = client.post(URL, body(component, store, qty="4", direction="OUT", reason="RETURN"), format="json")
        assert response.status_code == 409 and response.json()["code"] == "insufficient_stock"
        assert response.json()["errors"] == {"qty": ["Available: 3.000."]}
        assert Movement.objects.count() == 1 and not events("inventory.movement_recorded")

    def test_stock_is_per_location(self, client, component, store):
        MovementFactory(component=component, location=LocationFactory(), qty="50")
        response = client.post(URL, body(component, store, qty="1", direction="OUT", reason="RETURN"), format="json")
        assert response.status_code == 409 and response.json()["code"] == "insufficient_stock"

    def test_adjust_with_a_note_may_go_negative(self, client, keeper, component, store):
        MovementFactory(component=component, location=store, qty="2")
        response = client.post(URL, body(component, store, qty="5", direction="OUT", reason="ADJUST", note="stock count 2026-09-29: 3 short"), format="json")
        assert response.status_code == 201, response.json()
        assert response.json()["balance_after"] == "-3.000" and response.json()["negative_override"] is True
        entry = AuditLog.objects.get(action="inventory.movement_recorded")
        assert entry.after["negative_override"] is True and entry.note.startswith("negative-stock override")

    @pytest.mark.parametrize("extra", [{"reason": "PURCHASE"}, {"reason": "RETURN"}, {"reason": "ADJUST", "note": "partial recount"}])
    def test_an_in_into_a_negative_balance_is_never_refused(self, client, component, store, extra):
        """The no-negative rule is about OUTs: stock arriving at a location already below zero must be booked even
        when the balance stays negative (-5 + 2 = -3); refusing it would keep the ledger wrong for ever."""
        MovementFactory(component=component, location=store, qty="5", direction="OUT", reason="ADJUST", note="count: 5 short")
        response = client.post(URL, body(component, store, qty="2", direction="IN", **extra), format="json")
        assert response.status_code == 201, response.json()
        assert response.json()["balance_after"] == "-3.000" and response.json()["negative_override"] is False
        assert "negative_override" not in AuditLog.objects.get(action="inventory.movement_recorded").after

    def test_an_out_from_a_negative_balance_is_still_refused(self, client, component, store):
        MovementFactory(component=component, location=store, qty="5", direction="OUT", reason="ADJUST", note="count: 5 short")
        response = client.post(URL, body(component, store, qty="1", direction="OUT", reason="RETURN"), format="json")
        assert response.status_code == 409 and response.json()["code"] == "insufficient_stock"
        assert response.json()["errors"] == {"qty": ["Available: -5.000."]}

    def test_adjust_needs_a_note(self, client, component, store):
        response = client.post(URL, body(component, store, qty="5", direction="OUT", reason="ADJUST", note="  "), format="json")
        assert response.status_code == 400 and "note" in response.json()["errors"]

    def test_adjust_in_with_a_note(self, client, component, store):
        response = client.post(URL, body(component, store, qty="1", direction="IN", reason="ADJUST", note="found in the van"), format="json")
        assert response.status_code == 201 and response.json()["balance_after"] == "1.000"

    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"qty": "0"}, "qty"),
            ({"qty": "-1"}, "qty"),
            ({"qty": "1.2345"}, "qty"),
            ({"qty": "abc"}, "qty"),
            ({"direction": "SIDEWAYS"}, "direction"),
            ({"reason": "THEFT"}, "reason"),
            ({"direction": "OUT", "reason": "PURCHASE"}, "direction"),
            ({"direction": "IN", "reason": "ISSUE_TO_PROJECT", "ref_type": "projects.project", "ref_uid": str(uuid.uuid4())}, "direction"),
            ({"direction": "OUT", "reason": "ISSUE_TO_PROJECT"}, "ref_uid"),
            ({"ref_type": "projects.project"}, "ref_uid"),
            ({"ref_uid": str(uuid.uuid4())}, "ref_type"),
            ({"ref_type": "Projects Project", "ref_uid": str(uuid.uuid4())}, "ref_type"),
            ({"ref_type": "procurement.batch_line", "ref_uid": str(uuid.uuid4())}, "ref_type"),
            ({"component": str(uuid.uuid4())}, "component"),
            ({"location": str(uuid.uuid4())}, "location"),
            ({"at": "2099-01-01T00:00:00+05:30"}, "at"),
        ],
    )
    def test_validation(self, client, component, store, overrides, field):
        response = client.post(URL, {**body(component, store), **overrides}, format="json")
        assert response.status_code == 400, response.json()
        assert response.json()["code"] == "validation_error" and field in response.json()["errors"]
        assert not Movement.objects.exists()

    def test_deleted_component_or_location_is_refused(self, client, component, store):
        gone = ComponentFactory()
        gone.soft_delete()
        assert "component" in client.post(URL, body(gone, store), format="json").json()["errors"]
        closed = LocationFactory()
        closed.soft_delete()
        assert "location" in client.post(URL, body(component, closed), format="json").json()["errors"]

    def test_the_remaining_stock_of_a_deleted_component_can_be_cleared(self, client, store):
        """A soft-deleted component can still hold stock (received after its batch was committed, or deleted while the
        ledger was switched off). Moving that stock out must stay possible — otherwise it sits in the balances for ever
        and its location can never be deleted — but nothing else may move."""
        gone, short = ComponentFactory(), ComponentFactory()
        MovementFactory(component=gone, location=store, qty="5")
        MovementFactory(component=short, location=store, qty="2", direction="OUT", reason="ADJUST", note="count: 2 short")
        gone.soft_delete()
        short.soft_delete()

        more = client.post(URL, body(gone, store, qty="1"), format="json")
        assert more.status_code == 400 and "component" in more.json()["errors"]
        beyond = client.post(URL, body(gone, store, qty="6", direction="OUT", reason="ADJUST", note="write off"), format="json")
        assert beyond.status_code == 400 and "component" in beyond.json()["errors"]
        elsewhere = client.post(URL, body(gone, LocationFactory(), qty="1", direction="OUT", reason="ADJUST", note="x"), format="json")
        assert elsewhere.status_code == 400 and "component" in elsewhere.json()["errors"]

        project = str(uuid.uuid4())
        issued = client.post(URL, body(gone, store, qty="3", direction="OUT", reason="ISSUE_TO_PROJECT", ref_type="projects.project", ref_uid=project), format="json")
        assert issued.status_code == 201, issued.json()
        written_off = client.post(URL, body(gone, store, qty="2", direction="OUT", reason="ADJUST", note="scrapped"), format="json")
        assert written_off.status_code == 201 and written_off.json()["balance_after"] == "0.000"
        found = client.post(URL, body(short, store, qty="2", direction="IN", reason="ADJUST", note="found"), format="json")
        assert found.status_code == 201 and found.json()["balance_after"] == "0.000"
        assert client.delete(f"/api/v1/inventory/locations/{store.uid}/").status_code == 204

    def test_backdated_movement(self, client, component, store):
        at = (timezone.now() - dt.timedelta(days=3)).replace(microsecond=0)
        response = client.post(URL, body(component, store, at=at.isoformat()), format="json")
        assert response.status_code == 201
        assert Movement.objects.get(uid=response.json()["uid"]).at == at


class TestList:
    def test_filters_ordering_and_no_n_plus_one(self, client, component, store, django_assert_max_num_queries):
        other_location, other_component = LocationFactory(), ComponentFactory()
        project = uuid.uuid4()
        MovementFactory(component=component, location=store, qty="10")
        MovementFactory(component=component, location=store, qty="2", direction="OUT", reason="ISSUE_TO_PROJECT", ref_type="projects.project", ref_uid=project)
        MovementFactory(component=other_component, location=other_location, qty="7")
        MovementFactory(component=other_component, location=store, qty="1")
        MovementFactory(component=other_component, location=store, qty="1", at=timezone.now() - dt.timedelta(days=40))
        with django_assert_max_num_queries(8):
            rows = client.get(URL).json()["results"]
        assert len(rows) == 5 and rows[-1]["qty"] == "1.000"  # newest first; the 40-day-old one last
        assert client.get(URL, {"component": str(component.uid)}).json()["count"] == 2
        assert client.get(URL, {"location": str(store.uid)}).json()["count"] == 4
        assert client.get(URL, {"direction": "OUT"}).json()["count"] == 1
        assert client.get(URL, {"reason": ["ISSUE_TO_PROJECT", "PURCHASE"]}).json()["count"] == 5
        assert client.get(URL, {"ref_type": "projects.project", "ref_uid": str(project)}).json()["count"] == 1
        since = (timezone.localdate() - dt.timedelta(days=10)).isoformat()
        assert client.get(URL, {"date_from": since}).json()["count"] == 4
        assert client.get(URL, {"filter[date_to]": since}).json()["count"] == 1
        assert client.get(URL, {"search": "PNL-0001"}).json()["count"] == 2
        assert [row["qty"] for row in client.get(URL, {"ordering": "qty"}).json()["results"]][0] == "1.000"

    def test_the_query_count_does_not_grow_with_the_rows(self, client):
        """Every row with its own component, category, location and recorder: the page costs the same as one row."""

        def page_queries() -> int:
            with CaptureQueriesContext(connection) as captured:
                assert client.get(URL).status_code == 200
            return len(captured)

        MovementFactory(by=UserFactory())
        page_queries()  # warm the flag and session caches
        one = page_queries()
        for _ in range(5):
            MovementFactory(by=UserFactory(), component=ComponentFactory(category=CategoryFactory()))
        assert client.get(URL).json()["count"] == 6 and page_queries() == one

    def test_list_shape(self, client, component, store):
        MovementFactory(component=component, location=store, qty=Decimal("3.25"), note="n")
        [row] = client.get(URL).json()["results"]
        assert set(row) == {"uid", "component", "location", "qty", "direction", "reason", "ref_type", "ref_uid", "at", "by", "note", "created_at"}
        assert row["by"] is None and row["qty"] == "3.250"
