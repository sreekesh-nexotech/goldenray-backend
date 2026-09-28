"""Outbox: leads consumes ``quotations.issued`` (open leads of the customer → CONVERTED); dashboard counters."""

import pytest

from core.dashboard import counters_for
from core.outbox import emit
from customers.tests.factories import CustomerFactory
from leads.models import Lead, LeadEvent
from leads.tests.factories import AffiliateApplicationFactory, LeadFactory, WarrantyRequestFactory

pytestmark = pytest.mark.django_db


def test_quotation_issued_converts_the_customers_open_leads(drain_outbox):
    customer = CustomerFactory()
    open_lead = LeadFactory(customer=customer, status=Lead.Status.CONTACTED)
    lost = LeadFactory(customer=customer, status=Lead.Status.LOST, lost_reason="price")
    other = LeadFactory()
    emit("quotations.issued", {"quotation_uid": "7f3a9c21-0000-4000-8000-000000000001", "customer_uid": str(customer.uid)}, aggregate_type="quotations.quotation")
    drain_outbox()
    open_lead.refresh_from_db(), lost.refresh_from_db(), other.refresh_from_db()
    assert (open_lead.status, lost.status, other.status) == ("CONVERTED", "LOST", "NEW")
    event = LeadEvent.objects.get(lead=open_lead, event="CONVERTED")
    assert event.by is None and "quotation issued" in event.data["reason"]
    emit("quotations.issued", {"customer_uid": str(customer.uid)}, aggregate_type="quotations.quotation")
    drain_outbox()  # idempotent
    assert LeadEvent.objects.filter(lead=open_lead, event="CONVERTED").count() == 1


def test_quotation_issued_without_a_known_customer_is_ignored(drain_outbox):
    emit("quotations.issued", {"quotation_uid": "x"}, aggregate_type="quotations.quotation")
    emit("quotations.issued", {"customer_uid": "7f3a9c21-0000-4000-8000-000000000099"}, aggregate_type="quotations.quotation")
    result = drain_outbox()
    assert result["failed"] == 0


def test_dashboard_counters_follow_the_record_scope(make_user):
    seller = make_user(grants={"leads": ["view"]}, scopes={"leads": "owned"})
    LeadFactory(assignee=seller)
    LeadFactory()
    AffiliateApplicationFactory(assignee=seller)
    WarrantyRequestFactory()
    assert counters_for(seller)["leads"] == {"new": 1, "open": 1, "affiliate_applications_new": 1, "warranty_requests_open": 0}
    head = make_user(grants={"leads": ["view"], "customers": ["view"]}, scopes={"leads": "all", "customers": "all"})
    CustomerFactory()
    counters = counters_for(head)
    assert counters["leads"]["new"] == 2 and counters["leads"]["warranty_requests_open"] == 1 and counters["customers"] == {"total": 1, "new_30d": 1}
