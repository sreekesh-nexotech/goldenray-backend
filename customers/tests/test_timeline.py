"""customers/<uid>/timeline/ — registry providers, permission/scope filtering, time paging."""

import datetime as dt

import pytest
from django.utils import timezone

from customers.services import merge, timeline
from customers.tests.factories import CustomerFactory, CustomerNoteFactory
from leads.models import LeadEvent
from leads.tests.factories import LeadFactory, WarrantyRequestFactory

pytestmark = pytest.mark.django_db


def url(customer):
    return f"/api/v1/customers/{customer.uid}/timeline/"


def test_entries_from_every_provider_newest_first(auth_client, make_user):
    user = make_user(grants={"customers": ["view"], "leads": ["view"]}, scopes={"customers": "all", "leads": "all"})
    customer = CustomerFactory()
    lead = LeadFactory(customer=customer)
    LeadEvent.objects.create(lead=lead, event=LeadEvent.Event.CREATED)
    LeadEvent.objects.create(lead=lead, event=LeadEvent.Event.CONVERTED, at=timezone.now() + dt.timedelta(minutes=1))
    WarrantyRequestFactory(customer=customer)
    CustomerNoteFactory(customer=customer, body="Call after 5")
    merge.merge_customers(CustomerFactory(), into=customer, user=user)
    body = auth_client(user).get(url(customer)).json()
    kinds = [entry["kind"] for entry in body["results"]]
    assert set(kinds) == {"customers.created", "customers.note", "customers.merged", "leads.received", "leads.converted", "leads.warranty_request"}
    ats = [entry["at"] for entry in body["results"]]
    assert ats == sorted(ats, reverse=True) and body["next_before"] is None
    # Later sales contexts (site_inspections, …) register their own providers; these are the ones present on integration.
    assert {"agreements.agreements": "agreements", "customers.record": "customers", "leads.leads": "leads", "leads.warranty_requests": "leads", "quotations.quotations": "quotations"}.items() <= timeline.providers().items()


def test_entries_follow_the_viewers_permissions_and_scope(auth_client, make_user):
    customer = CustomerFactory()
    lead = LeadFactory(customer=customer)
    LeadEvent.objects.create(lead=lead, event=LeadEvent.Event.CREATED)
    only_customers = make_user(grants={"customers": ["view"]}, scopes={"customers": "all"})
    assert {entry["kind"] for entry in auth_client(only_customers).get(url(customer)).json()["results"]} == {"customers.created"}
    owned_leads = make_user(grants={"customers": ["view"], "leads": ["view"]}, scopes={"customers": "all", "leads": "owned"})
    assert {entry["kind"] for entry in auth_client(owned_leads).get(url(customer)).json()["results"]} == {"customers.created"}


def test_paging_by_time(auth_client, make_user):
    user = make_user(grants={"customers": ["view"]}, scopes={"customers": "all"})
    customer = CustomerFactory(created_at=timezone.now() - dt.timedelta(days=10))
    for day in range(5):
        CustomerNoteFactory(customer=customer, created_at=timezone.now() - dt.timedelta(days=day))
    client = auth_client(user)
    first = client.get(url(customer), {"limit": 2}).json()
    assert len(first["results"]) == 2 and first["next_before"]
    second = client.get(url(customer), {"limit": 2, "before": first["next_before"]}).json()
    third = client.get(url(customer), {"limit": 2, "before": second["next_before"]}).json()
    assert [len(page["results"]) for page in (first, second, third)] == [2, 2, 2] and third["next_before"] is None
    assert third["results"][-1]["kind"] == "customers.created"
    assert client.get(url(customer), {"limit": 0}).status_code == 400
    assert client.get(url(customer), {"before": "yesterday"}).status_code == 400


def test_registry_validates_modules():
    with pytest.raises(ValueError):
        timeline.register("x", module="nope")
