"""Quotation rows without pricing: list shape and query budget, record scope, accept/cancel/expire, the Beat task,
registrations (timeline, merge, documents access, dashboard, catalog usage), and the table constraints."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.errors import DomainError
from core.models import OutboxEvent
from customers.tests.factories import CustomerFactory
from quotations.models import BomSnapshot, DiscountRequest, EmailLog, Quotation, QuotationStatus, Version, VersionStatus
from quotations.services import lifecycle, quotations
from quotations.tests.factories import QuotationFactory, VersionFactory, issued_quotation

pytestmark = pytest.mark.django_db
BASE = "/api/v1/quotations/"


def test_list_shape_and_query_budget(head, head_user, django_assert_max_num_queries):
    for _ in range(12):
        issued_quotation(owner=head_user)
    with django_assert_max_num_queries(8):
        response = head.get(BASE, {"page_size": 50})
    assert response.status_code == 200 and response.json()["count"] == 12
    row = response.json()["results"][0]
    assert set(row) >= {"uid", "number", "status", "customer", "owner", "current_version", "valid_until", "legacy", "version"}
    assert row["current_version"]["status"] == "ISSUED" and row["customer"]["code"]
    with django_assert_max_num_queries(10):
        assert head.get(f"{BASE}{row['uid']}/").json()["versions"][0]["number"] == 1
    assert head.get(BASE, {"search": row["number"]}).json()["count"] == 1
    assert head.get(BASE, {"legacy": "false"}).json()["count"] == 12
    assert head.get(BASE, {"ordering": "number"}).status_code == 200


def test_list_requires_authentication_and_permission(api_client, outsider):
    assert api_client.get(BASE).status_code == 401
    assert outsider.get(BASE).status_code == 403


def test_owned_scope(executive, executive_user, head_user):
    mine = issued_quotation(owner=executive_user)
    theirs = issued_quotation(owner=head_user)
    assert [row["uid"] for row in executive.get(BASE).json()["results"]] == [str(mine.uid)]
    assert executive.get(f"{BASE}{theirs.uid}/").status_code == 404
    assert executive.post(f"{BASE}{theirs.uid}/accept/", {}, format="json").status_code == 404
    assert executive.get(f"{BASE}{mine.uid}/history/").status_code == 200


def test_accept_and_cancel_rules(executive, executive_user):
    quotation = issued_quotation(owner=executive_user)
    accepted = executive.post(f"{BASE}{quotation.uid}/accept/", {"expected_version": quotation.version}, format="json")
    assert accepted.status_code == 200 and accepted.json()["status"] == "ACCEPTED"
    assert executive.post(f"{BASE}{quotation.uid}/accept/", {}, format="json").json()["code"] == "quotation_not_issued"
    event = OutboxEvent.objects.get(event_type="quotations.accepted", aggregate_uid=quotation.uid)
    assert event.payload["quotation_uid"] == str(quotation.uid) and event.payload["final_price"] == "229000.00" and event.payload["language"] == "en"
    draft = QuotationFactory(owner=executive_user)
    VersionFactory(quotation=draft)
    assert executive.post(f"{BASE}{draft.uid}/cancel/", {"reason": "no"}, format="json").json()["status"] == "CANCELLED"
    assert executive.post(f"{BASE}{draft.uid}/cancel/", {"reason": "again"}, format="json").json()["code"] == "quotation_closed"
    with pytest.raises(DomainError) as error:
        lifecycle.cancel(Quotation(pk=10**9), user=None, reason="x")
    assert error.value.code == "not_found"


def test_expiry_task_and_timeline(head_user, head, customer):
    from quotations.tasks import expire_quotations

    due = issued_quotation(owner=head_user, customer=customer, valid_until=timezone.localdate() - dt.timedelta(days=1))
    fresh = issued_quotation(owner=head_user, customer=customer)
    assert expire_quotations() == [due.number]
    due.refresh_from_db()
    assert due.status == QuotationStatus.EXPIRED and due.expired_at is not None
    assert Quotation.objects.get(pk=fresh.pk).status == QuotationStatus.ISSUED
    assert lifecycle.expire_due(timezone.localdate() + dt.timedelta(days=30)) == [fresh.number]
    lifecycle.cancel(due, user=head_user, reason="lost to competitor")
    accepted = issued_quotation(owner=head_user, customer=customer, status=QuotationStatus.ACCEPTED)
    body = head.get(f"/api/v1/customers/{customer.uid}/timeline/").json()
    kinds = [entry["kind"] for entry in body["results"]]
    assert kinds.count("quotations.created") == 3 and kinds.count("quotations.issued") == 3
    assert "quotations.cancelled" in kinds and "quotations.accepted" in kinds
    assert f"Quotation {accepted.number} accepted" in [entry["title"] for entry in body["results"]]


def test_timeline_hides_quotations_outside_the_scope(auth_client, make_user, head_user, customer):
    issued_quotation(owner=head_user, customer=customer)
    viewer = make_user(grants={"customers": ["view"], "quotations": ["view"]}, scopes={"customers": "all", "quotations": "owned"})
    kinds = [entry["kind"] for entry in auth_client(viewer).get(f"/api/v1/customers/{customer.uid}/timeline/").json()["results"]]
    assert not [kind for kind in kinds if kind.startswith("quotations.")]


def test_customer_merge_moves_quotations_and_blocks_delete(head_user):
    from customers.services import merge

    source, target = CustomerFactory(), CustomerFactory()
    quotation = issued_quotation(owner=head_user, customer=source)
    assert merge.blocking_references(source) == {"quotations.quotation.customer": 1}
    merge.merge_customers(source, into=target, user=head_user)
    assert Quotation.objects.get(pk=quotation.pk).customer_id == target.pk


def test_documents_access_follows_the_quotation_scope(executive_user, head_user):
    from quotations.services.registrations import document_visible

    mine = issued_quotation(owner=executive_user)
    theirs = issued_quotation(owner=head_user)
    assert document_visible(executive_user, mine.current_version.uid) is True
    assert document_visible(executive_user, theirs.current_version.uid) is False
    assert document_visible(head_user, theirs.current_version.uid) is True


def test_dashboard_counters_and_component_usage(head_user, executive_user):
    from core.dashboard import counters_for
    from quotations.services.registrations import component_usage

    issued_quotation(owner=executive_user)
    QuotationFactory(owner=executive_user)
    issued_quotation(owner=head_user, status=QuotationStatus.ACCEPTED)
    assert counters_for(head_user)["quotations"] == {"drafts": 1, "issued": 1, "accepted": 1}
    assert counters_for(executive_user)["quotations"] == {"drafts": 1, "issued": 1, "accepted": 0}
    quotation = issued_quotation(owner=head_user)
    BomSnapshot.objects.create(quotation_version=quotation.current_version, tier="VALUE", is_primary=True, lines=[{"componentId": "PNL-1", "quantity": 6}], record={})
    usage = list(component_usage(type("C", (), {"sku": "PNL-1"})()))
    assert usage == [{"object_type": "quotations.quotation", "object_uid": quotation.uid, "label": f"Quotation {quotation.number}", "status": "ISSUED"}]


def test_no_pack_release(executive, customer):
    from quotations.tests.conftest import quotation_data

    response = executive.post(BASE, quotation_data(customer), format="json")
    assert response.status_code == 409 and response.json()["code"] == "no_pack_release"
    assert executive.get(f"{BASE}system-options/").json()["code"] == "no_pack_release"


def test_legacy_version_documents(head, head_user, document_storage):
    from documents.models import RenderJob

    quotation = issued_quotation(owner=head_user)
    url = f"{BASE}{quotation.uid}/versions/1/"
    assert head.get(f"{url}document/").json()["code"] == "document_not_ready"
    rendered = head.post(f"{url}render/", {"language": "ml"}, format="json")
    assert rendered.status_code == 200 and rendered.json()["status"] == "QUEUED" and rendered.json()["payload_sha256"] == quotation.current_version.document_payload_sha256
    again = head.post(f"{url}render/", {"language": "ml"}, format="json")
    assert again.json()["uid"] == rendered.json()["uid"]
    assert Version.objects.get(pk=quotation.current_version_id).document_job_ml.uid == RenderJob.objects.get(uid=rendered.json()["uid"]).uid
    assert head.post(f"{url}render/", {"language": "xx"}, format="json").status_code == 400
    assert head.get(f"{url}document/", {"language": "ml"}).status_code == 409  # still rendering
    from documents.services.jobs import run_job

    assert run_job(rendered.json()["uid"]) == RenderJob.Status.DONE
    link = head.get(f"{url}document/", {"language": "ml"})
    assert link.status_code == 200 and link.json()["url"]


# ── constraints ────────────────────────────────────────────────────────────────────────────────────────────────────


def _violates(fn) -> bool:
    try:
        with transaction.atomic():
            fn()
    except IntegrityError:
        return True
    return False


def test_quotation_constraints():
    customer = CustomerFactory()
    issued_quotation(customer=customer, number="GR-77")
    assert _violates(lambda: QuotationFactory(customer=customer, status="ISSUED", number="GR-77"))
    assert _violates(lambda: QuotationFactory(customer=customer, status="ISSUED", number=""))
    assert _violates(lambda: QuotationFactory(customer=customer, status="BOGUS"))
    assert _violates(lambda: QuotationFactory(customer=customer, source="BOGUS"))
    assert _violates(lambda: QuotationFactory(customer=customer, affiliate_ref="A-1"))
    assert _violates(lambda: QuotationFactory(customer=customer, source="DISTRICT"))
    assert _violates(lambda: QuotationFactory(customer=customer, accepted_at=timezone.now()))
    assert _violates(lambda: QuotationFactory(customer=customer, status="ACCEPTED", number="GR-78"))
    QuotationFactory(customer=customer, legacy_ref="q-1")
    assert _violates(lambda: QuotationFactory(customer=customer, legacy_ref="q-1"))


def test_version_constraints():
    quotation = QuotationFactory()
    VersionFactory(quotation=quotation)
    assert _violates(lambda: VersionFactory(quotation=quotation))  # number unique
    assert _violates(lambda: VersionFactory(quotation=quotation, number=2))  # one draft
    other = QuotationFactory()
    for bad in (
        {"status": "BOGUS"},
        {"system_type": "OFFGRID"},
        {"tier": "GOLD"},
        {"phase": "2P"},
        {"roof_type": "TILE"},
        {"subsidy_type": "other"},
        {"language": "fr"},
        {"battery_config": "5"},
        {"size_key": "3 kW"},
        {"future_size_key": "big"},
        {"number": 0},
        {"size_kw": Decimal("0")},
        {"distance_km": Decimal("-1")},
        {"offer_total": Decimal("-1")},
        {"legacy": False},
        {"status": "ISSUED"},
        {"document_payload": {"a": 1}},
    ):
        assert _violates(lambda bad=bad: VersionFactory(quotation=other, **bad)), bad


def test_discount_and_log_constraints():
    version = VersionFactory()
    DiscountRequest.objects.create(quotation_version=version, amount=Decimal("10"), reason="r")
    assert _violates(lambda: DiscountRequest.objects.create(quotation_version=version, amount=Decimal("10"), reason="r"))
    assert _violates(lambda: DiscountRequest.objects.create(quotation_version=VersionFactory(), amount=Decimal("0"), reason="r"))
    assert _violates(lambda: DiscountRequest.objects.create(quotation_version=VersionFactory(), amount=Decimal("1"), reason=""))
    assert _violates(lambda: DiscountRequest.objects.create(quotation_version=VersionFactory(), amount=Decimal("1"), reason="r", status="APPROVED"))
    assert _violates(lambda: EmailLog.objects.create(to="a@b.c"))
    assert _violates(lambda: EmailLog.objects.create(version=version, to="a@b.c", status="SENT"))
    assert _violates(lambda: EmailLog.objects.create(version=version, to="a@b.c", channel="SMS"))
    EmailLog.objects.create(legacy_ref="Q1", to="x", channel="LEGACY_LINK")
    assert _violates(lambda: EmailLog.objects.create(legacy_ref="Q1", to="x", channel="LEGACY_LINK"))


def test_snapshot_constraints():
    version = VersionFactory()
    BomSnapshot.objects.create(quotation_version=version, tier="VALUE", is_primary=True, record={})
    assert _violates(lambda: BomSnapshot.objects.create(quotation_version=version, tier="VALUE", record={}))
    assert _violates(lambda: BomSnapshot.objects.create(quotation_version=version, tier="BASE", is_primary=True, record={}))
    assert _violates(lambda: BomSnapshot.objects.create(quotation_version=version, tier="GOLD", record={}))
    assert _violates(lambda: BomSnapshot.objects.create(quotation_version=version, tier="BASE", engineering_status="MAYBE", record={}))


def test_get_version_not_found():
    quotation = QuotationFactory()
    with pytest.raises(DomainError) as error:
        quotations.get_version(quotation, 3)
    assert error.value.code == "version_not_found"
    assert VersionStatus.SUPERSEDED in VersionStatus.values
