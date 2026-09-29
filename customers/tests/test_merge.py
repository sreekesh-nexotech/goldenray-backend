"""customers/<uid>/merge/ — re-points every registered dependant, fills gaps, archives the source, audits, emits."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from audit.models import AuditLog
from core.errors import Conflict, DomainError, StaleVersion
from core.models import OutboxEvent
from customers.models import Customer, CustomerNote
from customers.services import merge
from customers.tests.factories import CustomerFactory, CustomerNoteFactory
from leads.models import Lead, WarrantyRequest
from leads.tests.factories import LeadFactory, WarrantyRequestFactory

pytestmark = pytest.mark.django_db


def url(customer, suffix="merge/"):
    return f"/api/v1/customers/{customer.uid}/{suffix}"


@pytest.fixture
def head(make_user):
    return make_user(grants={"customers": "*", "leads": ["view"]}, scopes={"customers": "all", "leads": "all"})


@pytest.fixture
def client(auth_client, head):
    return auth_client(head)


def test_registry_holds_every_reference():
    assert set(merge.dependants()) >= {"customers.customernote.customer", "customers.customer.merged_into", "leads.lead.customer", "leads.warrantyrequest.customer"}
    with pytest.raises(ImproperlyConfigured):
        merge.register_dependant(Lead, "assignee")
    with pytest.raises(ImproperlyConfigured):
        merge.register_dependant(Lead, "nope")


def test_merge_repoints_fills_and_archives(client, head, make_user):
    owner = make_user()
    source = CustomerFactory(phone_e164="+919811111111", email="old@example.com", pincode="682016", owner=owner)
    target = CustomerFactory(phone_e164="+919822222222", email="", owner=None, district="Kochi")
    earlier = CustomerFactory(phone_e164="+919833333333")
    merge.merge_customers(earlier, into=source, user=head)  # someone merged into the source before
    leads = LeadFactory.create_batch(2, customer=source)
    archived_lead = LeadFactory(customer=source)
    archived_lead.soft_delete()
    warranty = WarrantyRequestFactory(customer=source)
    CustomerNoteFactory.create_batch(2, customer=source)

    response = client.post(url(source), {"into_uid": str(target.uid), "expected_version": source.__class__.all_objects.get(pk=source.pk).version}, format="json")
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["uid"] == str(target.uid) and body["email"] == "old@example.com" and body["pincode"] == "682016" and body["district"] == "Kochi"
    assert body["alt_phone"] == "+919811111111" and body["owner"]["uid"] == str(owner.uid)

    source = Customer.all_objects.get(pk=source.pk)
    assert source.merged_into_id == target.pk and source.deleted_at is not None
    assert Customer.all_objects.get(pk=earlier.pk).merged_into_id == target.pk
    assert set(Lead.all_objects.filter(customer=target).values_list("pk", flat=True)) == {lead.pk for lead in leads} | {archived_lead.pk}
    assert WarrantyRequest.objects.get(pk=warranty.pk).customer_id == target.pk and WarrantyRequest.objects.get(pk=warranty.pk).version == 2
    assert CustomerNote.objects.filter(customer=target).count() == 2
    audit = AuditLog.objects.get(action="customers.merged", object_uid=target.uid)
    assert audit.after["repointed"] == {"customers.customer.merged_into": 1, "customers.customernote.customer": 2, "leads.lead.customer": 3, "leads.warrantyrequest.customer": 1}
    assert AuditLog.objects.filter(action="customers.merged_into", object_uid=source.uid).exists()
    event = OutboxEvent.objects.filter(event_type="customers.merged").latest("id")
    assert event.payload["source_uid"] == str(source.uid) and event.payload["target_uid"] == str(target.uid)
    # the source's number is free again
    assert client.post("/api/v1/customers/", {"name": "New", "phone": "+919811111111"}, format="json").status_code == 201
    # the merged record is gone from the API
    assert client.get(url(source, "")).status_code == 404


def test_errors(client, head, auth_client, make_user):
    source, target = CustomerFactory(), CustomerFactory()
    assert client.post(url(source), {"into_uid": str(source.uid)}, format="json").json()["code"] == "merge_into_self"
    assert client.post(url(source), {"into_uid": str(target.uid), "expected_version": 9}, format="json").json()["code"] == "stale_version"
    stale = client.post(url(source), {"into_uid": str(target.uid), "into_expected_version": 9}, format="json")
    assert stale.status_code == 409 and "into_expected_version" in stale.json()["errors"]
    missing = client.post(url(source), {"into_uid": "7f3a9c21-0000-4000-8000-000000000001"}, format="json")
    assert missing.status_code == 404 and "into_uid" in missing.json()["errors"]
    assert client.post(url(source), {}, format="json").status_code == 400
    owned = make_user(grants={"customers": "*"}, scopes={"customers": "owned"})
    mine = CustomerFactory(owner=owned)
    assert auth_client(owned).post(url(mine), {"into_uid": str(target.uid)}, format="json").status_code == 404  # target not visible


def test_service_guards():
    source, target = CustomerFactory(), CustomerFactory()
    with pytest.raises(DomainError):
        merge.merge_customers(source, into=source, user=None)
    with pytest.raises(StaleVersion):
        merge.merge_customers(source, into=target, user=None, into_expected_version=5)
    gone = CustomerFactory()
    gone.soft_delete()
    with pytest.raises(Conflict) as caught:
        merge.merge_customers(source, into=gone, user=None)
    assert caught.value.code == "customer_already_merged"


def test_merge_without_gaps_still_bumps_the_survivor(client):
    source = CustomerFactory(email="a@example.com")
    target = CustomerFactory(email="b@example.com", alt_phone="+919800000000", owner=None)
    body = client.post(url(source), {"into_uid": str(target.uid)}, format="json").json()
    assert body["email"] == "b@example.com" and body["version"] == 2
