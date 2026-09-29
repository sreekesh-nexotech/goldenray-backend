"""``agreements.issued`` / ``agreements.superseded`` handlers — driven by emitting the events (the agreements package
is built separately; only the payload contract of docs/decisions/site-inspections.md is shared)."""

import uuid

import pytest

from catalog.tests.factories import ComponentFactory
from core.models import OutboxEvent
from core.outbox import emit
from customers.tests.factories import CustomerFactory
from site_inspections.models import EquipmentAssessment, Inspection, LocationApproval, Snapshot
from site_inspections.models.choices import Status
from site_inspections.services import linking
from site_inspections.tests.factories import InspectionFactory, approved

pytestmark = pytest.mark.django_db


def payload(customer, **overrides):
    body = {
        "agreement_uid": str(uuid.uuid4()),
        "number": "AGR-2026-27-0001",
        "customer_uid": str(customer.uid),
        "lead_uid": None,
        "quotation_uid": str(uuid.uuid4()),
        "system_type": "ON_GRID",
        "size_kw": "5",
        "phase": "3P",
        "tier": "VALUE",
        "issued_at": "2026-09-29T10:00:00+05:30",
        "supersedes_uid": None,
        "fields": {"consumer_number": "1156780001234", "registered_phone": "9876500001", "wheeling_required": False, "address": "From agreement", "structure_type": "STANDARD"},
    }
    body.update(overrides)
    return body


def deliver(event_type, body, drain_outbox):
    emit(event_type, body, aggregate_type="agreements.agreement", aggregate_uid=body["agreement_uid"])
    outcome = drain_outbox()
    assert not OutboxEvent.objects.filter(parked_at__isnull=False).exists(), outcome
    return outcome


class TestIssued:
    def test_creates_an_agreement_inspection_when_there_is_no_pre_sale(self, drain_outbox):
        customer = CustomerFactory(address="Customer address", pincode="682001")
        panel = ComponentFactory()
        body = payload(customer, fields={"panel_uid": str(panel.uid), "panel_capacity_w": 545, "consumer_number": "C-1"})
        deliver("agreements.issued", body, drain_outbox)
        inspection = Inspection.objects.get(agreement_uid=body["agreement_uid"])
        assert inspection.origin == "AGREEMENT" and inspection.system_type == "ON_GRID" and inspection.status == "DRAFT" and inspection.agreement_version == 1
        assert inspection.quoted_panel == panel and inspection.quoted_size_kw == 5 and inspection.phase == "3P" and inspection.consumer_number == "C-1"
        assert inspection.address == "Customer address"
        snapshot = Snapshot.objects.get(inspection=inspection)
        assert snapshot.source == "AGREEMENT" and snapshot.data["system"]["tier"] == "VALUE" and "price" not in str(snapshot.data)

    def test_links_the_latest_open_pre_sale_by_customer_and_fills_only_empty_fields(self, drain_outbox):
        customer = CustomerFactory()
        older = InspectionFactory(customer=customer)
        pre_sale = InspectionFactory(customer=customer, consumer_number="VERIFIED-1", phase="1P", wheeling_required=None)
        InspectionFactory(customer=CustomerFactory(name=customer.name))  # same name, other customer: never matched
        body = payload(customer)
        deliver("agreements.issued", body, drain_outbox)
        pre_sale.refresh_from_db()
        assert pre_sale.agreement_uid == uuid.UUID(body["agreement_uid"]) and pre_sale.origin == "AGREEMENT"
        assert pre_sale.consumer_number == "VERIFIED-1" and pre_sale.phase == "1P" and pre_sale.wheeling_required is False
        assert pre_sale.registered_phone_e164 == "+919876500001"
        assert Inspection.objects.get(pk=older.pk).agreement_uid is None
        assert Snapshot.objects.filter(inspection=pre_sale, source="AGREEMENT").count() == 1

    def test_approved_pre_sale_is_not_converted_but_referenced(self, engineer, drain_outbox):
        customer = CustomerFactory()
        done = InspectionFactory(customer=customer, status=Status.APPROVED)
        body = payload(customer, system_type="HYBRID")
        deliver("agreements.issued", body, drain_outbox)
        created = Inspection.objects.get(agreement_uid=body["agreement_uid"])
        assert created.pk != done.pk and created.pre_sale_source == done and created.system_type == "HYBRID"

    def test_idempotent_and_extra_structure_ignored(self, drain_outbox):
        customer = CustomerFactory()
        body = payload(customer)
        deliver("agreements.issued", body, drain_outbox)
        assert linking.apply_agreement(body) is None
        assert linking.apply_agreement(payload(customer, kind="EXTRA_STRUCTURE")) is None
        assert Inspection.objects.filter(customer=customer).count() == 1

    @pytest.mark.parametrize("change", [{"system_type": "UNDECIDED"}, {"agreement_uid": "x"}, {"customer_uid": str(uuid.uuid4())}, {"fields": []}])
    def test_contract_violations_raise(self, change):
        with pytest.raises(linking.PayloadError):
            linking.apply_agreement(payload(CustomerFactory(), **change))

    def test_merged_customer_follows_the_survivor(self):
        survivor = CustomerFactory()
        merged = CustomerFactory()
        type(merged).all_objects.filter(pk=merged.pk).update(deleted_at="2026-09-01T00:00:00Z", merged_into=survivor)
        inspection = linking.apply_agreement(payload(merged))
        assert inspection.customer == survivor


class TestSuperseded:
    def test_refresh_bumps_version_and_sends_back_on_system_type_change(self, engineer, drain_outbox):
        inspection = approved(InspectionFactory(engineer=engineer, status=Status.IN_PROGRESS), engineer)
        old = uuid.uuid4()
        Inspection.objects.filter(pk=inspection.pk).update(origin="AGREEMENT", agreement_uid=old, agreement_version=1)
        body = payload(inspection.customer, system_type="HYBRID", supersedes_uid=str(old), number="AGR-2026-27-0002")
        deliver("agreements.superseded", body, drain_outbox)
        inspection.refresh_from_db()
        assert inspection.agreement_uid == uuid.UUID(body["agreement_uid"]) and inspection.agreement_version == 2 and inspection.agreement_number == "AGR-2026-27-0002"
        assert inspection.system_type == "HYBRID" and inspection.status == Status.REVISION_REQUIRED
        assert not EquipmentAssessment.objects.filter(inspection=inspection).exists()
        assert set(LocationApproval.objects.filter(inspection=inspection).values_list("status", flat=True)) == {"SUPERSEDED"}
        deliver("agreements.issued", body, drain_outbox)  # the same agreement's issued event: nothing more
        assert Snapshot.objects.filter(inspection=inspection, source="AGREEMENT").count() == 1

    def test_same_system_type_keeps_the_status(self, engineer):
        inspection = InspectionFactory(engineer=engineer, status=Status.COMPLETED, system_type="ON_GRID", origin="AGREEMENT", agreement_uid=uuid.uuid4(), agreement_version=3)
        refreshed = linking.apply_agreement(payload(inspection.customer, supersedes_uid=str(inspection.agreement_uid), version=7))
        assert refreshed.pk == inspection.pk and refreshed.status == Status.COMPLETED and refreshed.agreement_version == 7
