"""Cross-context contracts: the ``agreements.issued`` / ``agreements.superseded`` payload consumed by site_inspections,
and the ``quotations.accepted`` handler (a DRAFT Purchase Agreement, idempotent)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from agreements.models import Agreement, AgreementKind, AgreementStatus
from agreements.services import agreements, issuing
from agreements.tests.factories import issued_version
from core.models import OutboxEvent

# The contract agreed with site_inspections (docs/decisions/site-inspections.md → "Event contracts"): these keys,
# exactly, with these types. ``kind`` and ``version`` are the two optional keys that contract names.
TOP_LEVEL = {
    "agreement_uid": str,
    "number": str,
    "kind": str,
    "version": int,
    "customer_uid": str,
    "lead_uid": (str, type(None)),
    "quotation_uid": (str, type(None)),
    "system_type": str,
    "size_kw": (str, type(None)),
    "phase": (str, type(None)),
    "tier": (str, type(None)),
    "issued_at": str,
    "supersedes_uid": (str, type(None)),
    "fields": dict,
}
FIELDS = {
    "consumer_number": (str, type(None)),
    "registered_phone": (str, type(None)),
    "wheeling_required": (bool, type(None)),
    "address": (str, type(None)),
    "pincode": (str, type(None)),
    "district": (str, type(None)),
    "location": (str, type(None)),
    "panel_uid": (str, type(None)),
    "panel_capacity_w": (int, type(None)),
    "inverter_uid": (str, type(None)),
    "battery_uid": (str, type(None)),
    "structure_type": (str, type(None)),
    "structure_material": (str, type(None)),
    "quotation_version_uid": (str, type(None)),
}
COMMERCIAL_WORDS = ("price", "discount", "cost", "fee", "offer", "amount", "margin", "bank")


def _assert_contract(payload: dict) -> None:
    assert set(payload) == set(TOP_LEVEL)
    for key, types in TOP_LEVEL.items():
        assert isinstance(payload[key], types), key
    assert set(payload["fields"]) == set(FIELDS)
    for key, types in FIELDS.items():
        assert isinstance(payload["fields"][key], types), key
    assert payload["system_type"] in ("ON_GRID", "HYBRID") and payload["phase"] in ("1P", "3P", None) and payload["tier"] in ("BASE", "VALUE", "PREMIUM", None)
    for key in (*payload, *payload["fields"]):
        assert not any(word in key for word in COMMERCIAL_WORDS), f"{key} is commercial; the engineer never sees prices"
    uuid.UUID(payload["agreement_uid"])
    uuid.UUID(payload["customer_uid"])


def test_issued_and_superseded_payload_contract(head_user, version, company, document_storage):
    from leads.tests.factories import LeadFactory

    customer = version.quotation.customer
    customer.lead = LeadFactory()
    customer.save()
    draft = agreements.create_from_quotation(user=head_user, data={"quotation_version_uid": version.uid})
    agreements.update_draft(draft, user=head_user, data={"consumer_number": "1155678", "registered_phone": "9847012345", "wheeling_required": False})
    issued = issuing.issue(draft, user=head_user)
    payload = OutboxEvent.objects.get(event_type="agreements.issued").payload
    _assert_contract(payload)
    assert payload == {
        "agreement_uid": str(issued.uid),
        "number": issued.number,
        "kind": "PURCHASE_AGREEMENT",
        "version": 1,
        "customer_uid": str(customer.uid),
        "lead_uid": str(customer.lead.uid),
        "quotation_uid": str(version.quotation.uid),
        "system_type": "ON_GRID",
        "size_kw": "3.00",
        "phase": "1P",
        "tier": "VALUE",
        "issued_at": issued.issued_at.isoformat(),
        "supersedes_uid": None,
        "fields": {
            "consumer_number": "1155678",
            "registered_phone": "+919847012345",
            "wheeling_required": False,
            "address": "Test Street 1",
            "pincode": "688001",
            "district": "Alappuzha",
            "location": None,
            "panel_uid": str(issued.panel.uid),
            "panel_capacity_w": 550,
            "inverter_uid": str(issued.inverter.uid),
            "battery_uid": None,
            "structure_type": "FLAT",
            "structure_material": "2.5×1.5 Square Tube 16 Gauge GP",
            "quotation_version_uid": str(version.uid),
        },
    }
    revision = agreements.supersede(issued, user=head_user)
    reissued = issuing.issue(revision, user=head_user)
    superseded = OutboxEvent.objects.get(event_type="agreements.superseded").payload
    _assert_contract(superseded)
    assert superseded["agreement_uid"] == str(reissued.uid) and superseded["supersedes_uid"] == str(issued.uid) and superseded["version"] == 2
    assert OutboxEvent.objects.filter(event_type="agreements.issued").count() == 2


def test_blank_agreement_payload_has_no_quotation(head_user, world, company, document_storage):
    customer = issued_version().quotation.customer
    data = {"kind": "SALE_ORDER", "customer_uid": customer.uid, "capacity_kw": Decimal("5"), "phase": "3P", "variant": "BASE", "system_type": "HYBRID", "panel_uid": world["panel"].uid}
    draft = agreements.create_blank(user=head_user, data={**data, "inverter_uid": world["inverter"].uid, "battery_uid": world["battery"].uid, "original_price": Decimal("500000")})
    issuing.issue(draft, user=head_user)
    payload = OutboxEvent.objects.get(event_type="agreements.issued").payload
    _assert_contract(payload)
    assert payload["quotation_uid"] is None and payload["fields"]["quotation_version_uid"] is None and payload["system_type"] == "HYBRID"
    assert payload["fields"]["battery_uid"] == str(world["battery"].uid) and payload["kind"] == "SALE_ORDER"


# ── quotations.accepted ─────────────────────────────────────────────────────────────────────────────────────────────


def _accept(version, drain_outbox):
    from quotations.services.lifecycle import accept

    accept(version.quotation, user=None)
    drain_outbox()


def test_quotation_accepted_drafts_a_purchase_agreement_once(version, drain_outbox, head_user):
    _accept(version, drain_outbox)
    agreement = Agreement.objects.get(quotation_version=version)
    assert agreement.kind == AgreementKind.PURCHASE_AGREEMENT and agreement.status == AgreementStatus.DRAFT
    assert agreement.owner == head_user and agreement.created_by is None and agreement.final_price == Decimal("222000.00")
    event = OutboxEvent.objects.get(event_type="quotations.accepted")
    agreements.draft_from_accepted_quotation(event.payload)
    agreements.draft_from_accepted_quotation(event.payload)
    assert Agreement.objects.filter(quotation_version=version).count() == 1


def test_quotation_accepted_after_a_manual_agreement_creates_nothing(version, drain_outbox, head_user):
    agreements.create_from_quotation(user=head_user, data={"quotation_version_uid": version.uid})
    _accept(version, drain_outbox)
    assert Agreement.objects.filter(quotation_version=version).count() == 1


@pytest.mark.django_db
def test_quotation_accepted_with_an_unknown_or_superseded_version():
    assert agreements.draft_from_accepted_quotation({"version_uid": str(uuid.uuid4())}) is None
    version = issued_version()
    type(version).objects.filter(pk=version.pk).update(status="SUPERSEDED")
    assert agreements.draft_from_accepted_quotation({"version_uid": str(version.uid), "language": "ml"}) is None
    assert not Agreement.objects.exists()


def test_the_handler_is_registered():
    from agreements.events import draft_purchase_agreement
    from core.outbox import handlers_for

    assert draft_purchase_agreement in handlers_for("quotations.accepted")
