"""``site-inspections/`` list / detail / create / archive, the engineer queue: auth, permission, scope, validation."""

import pytest

from customers.tests.factories import CustomerFactory
from site_inspections.models import Inspection, Snapshot
from site_inspections.models.choices import Status
from site_inspections.tests.factories import InspectionFactory

pytestmark = pytest.mark.django_db
BASE = "/api/v1/site-inspections/"


class TestAuth:
    def test_anonymous_is_401(self, api_client):
        assert api_client.get(BASE).status_code == 401

    def test_without_permission_is_403(self, auth_client, make_user):
        assert auth_client(make_user(grants={"customers": ["view"]})).get(BASE).status_code == 403

    def test_create_needs_create(self, auth_client, engineer):
        response = auth_client(engineer).post(BASE, {"customer_uid": str(CustomerFactory().uid)}, format="json")
        assert response.status_code == 403


class TestScope:
    def test_assigned_sees_only_own_inspections(self, auth_client, engineer):
        mine = InspectionFactory(engineer=engineer)
        InspectionFactory()
        body = auth_client(engineer).get(BASE).json()
        assert [row["uid"] for row in body["results"]] == [str(mine.uid)]
        assert auth_client(engineer).get(f"{BASE}{InspectionFactory().uid}/").status_code == 404

    def test_owned_sees_own_customers_and_own_creations(self, auth_client, sales):
        of_my_customer = InspectionFactory(customer=CustomerFactory(owner=sales))
        created_by_me = InspectionFactory(created_by=sales)
        InspectionFactory()
        uids = {row["uid"] for row in auth_client(sales).get(BASE).json()["results"]}
        assert uids == {str(of_my_customer.uid), str(created_by_me.uid)}

    def test_all_sees_everything_and_filters(self, auth_client, head):
        InspectionFactory(status=Status.IN_PROGRESS)
        InspectionFactory()
        client = auth_client(head)
        assert client.get(BASE).json()["count"] == 2
        assert client.get(BASE, {"status": "IN_PROGRESS"}).json()["count"] == 1
        assert client.get(BASE, {"unassigned": "true"}).json()["count"] == 2


class TestCreate:
    def test_pre_sale_inspection_with_number_and_snapshot(self, auth_client, sales):
        customer = CustomerFactory(owner=sales, address="Kochi", pincode="682020")
        response = auth_client(sales).post(BASE, {"customer_uid": str(customer.uid)}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["origin"] == "PRE_SALE" and body["status"] == "DRAFT" and body["system_type"] == "UNDECIDED"
        assert body["number"].startswith("SV-") and len(body["number"]) == len("SV-20260929-0001")
        assert body["address"] == "Kochi" and body["pincode"] == "682020"
        inspection = Inspection.objects.get(uid=body["uid"])
        assert Snapshot.objects.get(inspection=inspection).data["origin"] == "PRE_SALE"
        assert inspection.created_by == sales

    def test_numbers_are_sequential_per_day(self, auth_client, sales):
        customer = CustomerFactory(owner=sales)
        client = auth_client(sales)
        first = client.post(BASE, {"customer_uid": str(customer.uid)}, format="json").json()["number"]
        second = client.post(BASE, {"customer_uid": str(customer.uid)}, format="json").json()["number"]
        assert int(second[-4:]) == int(first[-4:]) + 1

    def test_customer_outside_scope_is_404(self, auth_client, sales):
        response = auth_client(sales).post(BASE, {"customer_uid": str(CustomerFactory().uid)}, format="json")
        assert response.status_code == 404 and response.json()["code"] == "customer_not_found"

    def test_validation_envelope(self, auth_client, sales):
        response = auth_client(sales).post(BASE, {"customer_uid": "nope", "pincode": "12"}, format="json")
        body = response.json()
        assert response.status_code == 400 and body["code"] == "validation_error" and {"customer_uid", "pincode"} <= set(body["errors"])

    def test_assigning_an_engineer_on_create_needs_assign(self, auth_client, sales, engineer):
        response = auth_client(sales).post(BASE, {"customer_uid": str(CustomerFactory(owner=sales).uid), "engineer_uid": str(engineer.uid)}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "assign_forbidden"

    def test_engineer_must_be_a_field_engineer(self, auth_client, admin, make_user):
        nobody = make_user(grants={"customers": ["view"]})
        response = auth_client(admin).post(BASE, {"customer_uid": str(CustomerFactory().uid), "engineer_uid": str(nobody.uid)}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "not_a_field_engineer"


class TestDetailAndArchive:
    def test_detail_has_every_stage_group_and_no_price(self, auth_client, head):
        inspection = InspectionFactory()
        body = auth_client(head).get(f"{BASE}{inspection.uid}/").json()
        assert {"road_access", "roof_type", "morning_shading", "neutral_link", "termination_point", "ac_cable_m", "site_suitability", "panel_width_m", "quoted_size_kw"} <= set(body)
        assert not [key for key in body if any(word in key for word in ("price", "discount", "cost", "margin"))]

    def test_archive_and_stale_version(self, auth_client, admin):
        inspection = InspectionFactory()
        client = auth_client(admin)
        assert client.delete(f"{BASE}{inspection.uid}/?expected_version=9").json()["code"] == "stale_version"
        assert client.delete(f"{BASE}{inspection.uid}/").status_code == 204
        assert Inspection.all_objects.get(pk=inspection.pk).deleted_at is not None

    def test_approved_inspection_cannot_be_archived(self, auth_client, admin):
        inspection = InspectionFactory(status=Status.APPROVED)
        response = auth_client(admin).delete(f"{BASE}{inspection.uid}/")
        assert response.status_code == 409 and response.json()["code"] == "inspection_read_only"

    def test_list_query_count_is_bounded(self, auth_client, head, django_assert_max_num_queries):
        for _ in range(8):
            InspectionFactory(engineer=head)
        client = auth_client(head)
        with django_assert_max_num_queries(12):
            assert client.get(BASE).status_code == 200


class TestEngineerQueue:
    def test_queue_lists_my_work_first(self, auth_client, engineer, django_assert_max_num_queries):
        later = InspectionFactory(engineer=engineer, status=Status.DRAFT)
        working = InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS)
        InspectionFactory(engineer=engineer, status=Status.INSTALLATION_READY, released_at="2026-09-29T10:00:00Z")
        InspectionFactory()
        client = auth_client(engineer)
        with django_assert_max_num_queries(12):
            body = client.get("/api/v1/engineer/site-inspections/").json()
        assert [row["uid"] for row in body["results"]] == [str(working.uid), str(later.uid)]

    def test_queue_needs_view(self, auth_client, make_user, api_client):
        assert api_client.get("/api/v1/engineer/site-inspections/").status_code == 401
        assert auth_client(make_user(grants={"customers": ["view"]})).get("/api/v1/engineer/site-inspections/").status_code == 403
