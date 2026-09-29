"""Wave 4c integration: agreements ↔ site_inspections ↔ projects, through the real outbox and the real handlers.

* an issued agreement's ``agreements.issued`` event is consumed by ``site_inspections.events`` (payload contract
  match: the inspection is created and linked, nothing is parked); a revision's ``agreements.superseded`` moves it;
* the released inspection's ``site_inspections.released`` event is accepted by projects' handler (auto-create on);
* the COST_CALCULATED validator registered by ``AgreementsConfig.ready`` accepts only a live ISSUED/ACCEPTED
  EXTRA_STRUCTURE agreement raised from the same inspection;
* the PA legacy importer gives imported agreements the uid a legacy PA inspection carries.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from agreements.models import Agreement
from agreements.services import agreements, issuing
from agreements.services.registrations import extra_structure_problem
from core.errors import DomainError
from core.models import OutboxEvent

ENGINEER = {"dashboard": ["view"], "site_inspections": ["view", "edit", "submit"], "media": ["create"], "customers": ["view"]}
HEAD = {"site_inspections": ["view", "assign", "approve", "release"], "customers": ["view"]}


@pytest.fixture
def engineer(make_user):
    return make_user(grants=ENGINEER, scopes={"site_inspections": "assigned"})


@pytest.fixture
def si_head(make_user):
    return make_user(grants=HEAD, scopes={"site_inspections": "all", "customers": "all"})


def _nothing_parked():
    parked = list(OutboxEvent.objects.filter(parked_at__isnull=False).values_list("event_type", "last_error"))
    assert parked == []


def _issued_purchase_agreement(user, version):
    draft = agreements.create_from_quotation(user=user, data={"quotation_version_uid": version.uid})
    agreements.update_draft(draft, user=user, data={"consumer_number": "1155678", "registered_phone": "9847012345", "wheeling_required": False})
    return issuing.issue(draft, user=user)


def test_issued_agreement_links_an_inspection_and_the_release_creates_the_project(head_user, version, company, document_storage, drain_outbox, engineer, si_head, settings):
    from projects.models import Project
    from site_inspections.models import Inspection
    from site_inspections.models.choices import Status
    from site_inspections.services import lifecycle
    from site_inspections.tests.factories import approved

    settings.PROJECTS_AUTO_CREATE_ON_RELEASE = True
    issued = _issued_purchase_agreement(head_user, version)
    drain_outbox()
    _nothing_parked()
    inspection = Inspection.objects.get(agreement_uid=issued.uid)
    assert inspection.origin == "AGREEMENT" and inspection.agreement_number == issued.number and inspection.customer_id == issued.customer_id
    assert inspection.system_type == "ON_GRID" and inspection.consumer_number == "1155678" and inspection.registered_phone_e164 == "+919847012345"
    assert inspection.quoted_size_kw == Decimal("3.000")

    # A revision: agreements.superseded (and agreements.issued) re-point the same inspection to the new agreement.
    revised = issuing.issue(agreements.supersede(issued, user=head_user), user=head_user)
    drain_outbox()
    _nothing_parked()
    inspection.refresh_from_db()
    assert inspection.agreement_uid == revised.uid and inspection.agreement_number == revised.number
    assert Inspection.objects.filter(customer=issued.customer).count() == 1

    # Released → projects' handler accepts what site_inspections emits.
    Inspection.objects.filter(pk=inspection.pk).update(status=Status.IN_PROGRESS, engineer=engineer)
    inspection = approved(Inspection.objects.get(pk=inspection.pk), engineer)
    lifecycle.release(inspection, user=si_head)
    drain_outbox()
    _nothing_parked()
    project = Project.objects.get(site_inspection_uid=inspection.uid)
    assert project.customer_id == issued.customer_id and project.agreement_uid == revised.uid
    assert project.quotation_version_uid == version.uid and project.size_kw == Decimal("3.00") and project.phase == "1P"


def test_cost_calculated_needs_an_issued_extra_structure_agreement_of_the_same_inspection(head_user, version, company, document_storage, drain_outbox, engineer, si_head, world):
    from site_inspections.models import AdditionalWorkItem, Inspection
    from site_inspections.models.choices import Status
    from site_inspections.services import work

    assert work._validator is extra_structure_problem  # registered by AgreementsConfig.ready()
    purchase = _issued_purchase_agreement(head_user, version)
    drain_outbox()
    inspection = Inspection.objects.get(agreement_uid=purchase.uid)
    Inspection.objects.filter(pk=inspection.pk).update(status=Status.IN_PROGRESS, engineer=engineer)
    inspection.refresh_from_db()
    item = work.create_item(inspection, user=engineer, data={"work_type": "WALKWAY", "customer_impacting": True})
    work.transition_item(item, user=si_head, status="ENGINEERING_REVIEW")

    def extra(source_uid):
        return agreements.create_blank(
            user=head_user,
            data={
                "kind": "EXTRA_STRUCTURE",
                "customer_uid": purchase.customer.uid,
                "source_type": "SITE_INSPECTION",
                "source_uid": source_uid,
                "base_agreement_uid": purchase.uid,
                "lines": [{"description": "Walkway", "quantity": Decimal("4"), "unit": "m", "unit_price": Decimal("1500"), "additional_work_item_uid": item.uid}],
            },
        )

    other_inspection = extra(uuid.uuid4())
    draft = extra(inspection.uid)
    for agreement_uid, message in (
        (uuid.uuid4(), "No such agreement."),
        ("not-a-uid", "Not a valid agreement uid."),
        (purchase.uid, "Not an EXTRA_STRUCTURE agreement."),
        (draft.uid, "The EXTRA_STRUCTURE agreement is DRAFT; it must be ISSUED or ACCEPTED."),
        (issuing.issue(other_inspection, user=head_user).uid, "The EXTRA_STRUCTURE agreement was not raised from this site inspection."),
    ):
        with pytest.raises(DomainError) as error:
            work.transition_item(AdditionalWorkItem.objects.get(pk=item.pk), user=si_head, status="COST_CALCULATED", agreement_uid=agreement_uid)
        assert error.value.code == "agreement_invalid" and error.value.errors["agreement_uid"] == [message]
    issued_extra = issuing.issue(draft, user=head_user)
    moved = work.transition_item(AdditionalWorkItem.objects.get(pk=item.pk), user=si_head, status="COST_CALCULATED", agreement_uid=issued_extra.uid)
    assert moved.status == "COST_CALCULATED" and moved.agreement_uid == issued_extra.uid
    # An EXTRA_STRUCTURE issue never creates or re-links an inspection (DV-127).
    drain_outbox()
    _nothing_parked()
    assert Inspection.objects.count() == 1 and Inspection.objects.get().agreement_uid == purchase.uid


@pytest.mark.django_db
def test_legacy_pa_agreements_take_the_site_inspection_link_uid(world, company):
    from agreements.services.legacy_import import import_pa_agreements
    from site_inspections.services.legacy_import import SI_AGREEMENT_NAMESPACE, agreement_uid

    record = {"id": "lx9k2", "type": 2, "typeName": "Sale Order", "customerName": "Ravi", "createdAt": "2026-03-02T10:00:00.000Z", "data": {"name": "Ravi", "size": "3 KW", "phase": "Single Phase"}}
    import_pa_agreements([record], profile="crs")
    imported = Agreement.objects.get(legacy_ref="crs/lx9k2")
    assert imported.uid == uuid.uuid5(SI_AGREEMENT_NAMESPACE, "PA:lx9k2") == agreement_uid("lx9k2")
    # The same record id in the other profile cannot take the uid twice.
    result = import_pa_agreements([{**record, "data": {**record["data"], "name": "Ravi K"}}], profile="admin")
    assert [violation["code"] for violation in result["violations"] if violation["code"] == "duplicate_record_id"] == ["duplicate_record_id"]
    assert Agreement.objects.get(legacy_ref="admin/lx9k2").uid != imported.uid
