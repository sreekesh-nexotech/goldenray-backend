"""End to end on the real Flarize data: a quotation priced and issued by the quotations services (PackRelease #1),
accepted (``quotations.accepted`` → the agreements handler drafts the Purchase Agreement), issued as an agreement.

Pins exactly what the quotation froze: the locked BOM's panel/inverter/structure (catalog components by SKU), the
version's size/phase/tier, its prices (customer total, offer + discounts, final price) and the KSEB fee of the
quotation's PriceRelease."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import transaction

from agreements.models import Agreement, AgreementKind
from agreements.services import issuing
from core.models import OutboxEvent


@pytest.fixture(scope="module")
def flarize_world(django_db_setup, django_db_blocker):
    from quotations.tests import flarize

    with django_db_blocker.unblock():
        atomic = transaction.atomic()
        atomic.__enter__()
        try:
            yield flarize.import_world()
        finally:
            transaction.set_rollback(True)
            atomic.__exit__(None, None, None)


def test_accepted_real_quotation_becomes_an_issued_purchase_agreement(flarize_world, db, make_user, company, document_storage, drain_outbox):
    from customers.tests.factories import CustomerFactory
    from pricing.tests.factories import StatutoryFeeFactory
    from quotations.services import lifecycle, quotations

    StatutoryFeeFactory(label="3 KW", capacity_kw_max=Decimal("3"), amount=Decimal("5400.00"))
    user = make_user(grants={"quotations": "*", "customers": "*", "agreements": "*"}, scopes={"quotations": "all", "customers": "all", "agreements": "all"})
    customer = CustomerFactory(
        name="Test Customer One", phone_e164="+919000000001", address="Test Street 1", pincode="688001", district="Alappuzha", current_bill=Decimal("6000"), bill_cycle="BIMONTHLY"
    )
    data = {
        "customer_uid": customer.uid,
        "system_type": "ONGRID",
        "tier": "VALUE",
        "size_key": "3",
        "roof_type": "FLAT",
        "distance_km": "60",
        "vehicle_type": "ACE",
        "subsidy_type": "residential",
        "language": "en",
    }
    quotation = quotations.create(user=user, data=data)
    version = quotations.issue(quotation.current_version, user=user)
    lifecycle.accept(quotation, user=user)
    drain_outbox()
    agreement = Agreement.objects.get(quotation_version=version)
    assert agreement.kind == AgreementKind.PURCHASE_AGREEMENT and agreement.owner == user and agreement.status == "DRAFT"
    rows = {row["role"]: row for row in version.document_payload["payload"]["bomSummary"]["rows"]}
    assert agreement.panel.sku == rows["PANEL"]["componentId"] and agreement.panel_qty == rows["PANEL"]["quantity"]
    assert agreement.panel_capacity_w == rows["PANEL"]["attributes"]["moduleWatt"] and agreement.panel_dcr is True
    assert agreement.inverter.sku == rows["INVERTER"]["componentId"] and agreement.inverter_type == "STRING"
    assert agreement.structure_material == rows["STRUCTURE"]["attributes"]["model"] and agreement.structure_template.slug == "flat_roof"
    assert (agreement.capacity_kw, agreement.phase, agreement.variant, agreement.system_type) == (version.size_kw, version.phase, "VALUE", "ON_GRID")
    assert agreement.final_price == version.final_price and agreement.discount == version.offer_total + version.discount_total
    assert agreement.original_price == version.customer_price_incl_gst + version.transport_extra
    # PriceRelease #1 pins no KSEB fee (the Flarize data has none): the current pricing row applies.
    assert version.price_release.payload["statutory_fees"] == []
    assert agreement.statutory_fee_amount == Decimal("5400.00") and agreement.statutory_fee is not None
    issued = issuing.issue(agreement, user=user)
    quotation.refresh_from_db()
    assert issued.number.startswith("AGR-") and issued.payload["quotation"]["number"] == quotation.number
    event = OutboxEvent.objects.get(event_type="agreements.issued")
    assert event.payload["quotation_uid"] == str(quotation.uid) and event.payload["fields"]["panel_uid"] == str(agreement.panel.uid)
