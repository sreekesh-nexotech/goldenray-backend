"""Agreement factories: catalog components, KSEB fees and an ISSUED quotation version whose frozen document names
them (the shape ``quotations.services.quotations.issue`` freezes: ``payload.bomSummary.rows`` with ``componentId`` =
SKU), so agreement tests need no PackRelease pipeline."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import factory
from django.utils import timezone

from agreements.models import Agreement, AgreementKind, AgreementStatus
from customers.tests.factories import CustomerFactory
from engines.frozen import sha256_hex


class AgreementFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Agreement

    kind = AgreementKind.SALE_ORDER
    customer = factory.SubFactory(CustomerFactory)
    status = AgreementStatus.DRAFT
    capacity_kw = Decimal("5")
    size_label = "5 KW"
    phase = "1P"
    variant = "VALUE"
    panel_label = "Waaree - Mono Perc Bifacial - DCR"
    inverter_brand = "Growatt"
    inverter_type = "STRING"
    original_price = Decimal("300000.00")
    final_price = Decimal("300000.00")


def catalog():
    """Panel ``p2`` (550 W DCR), string inverter ``i20``, battery ``b1`` and the flat-roof structure template."""
    from bom.tests.factories import StructureTemplateFactory
    from catalog.tests.factories import battery, inverter, panel

    return {
        "panel": panel(sku="p2", name="Adani Mono perc Bifacial 550W DCR", model="Adani Mono perc Bifacial 550W DCR", spec={"wattage_w": 550, "is_dcr": True}),
        "inverter": inverter(sku="i20", name="Sungrow SG 3.0RT", model="SG 3.0RT", spec={"kw": Decimal("3.000")}),
        "battery": battery(sku="b1", name="Battery 5 kWh", spec={"capacity_kwh": Decimal("5.00")}),
        "structure": StructureTemplateFactory(slug="flat_roof", name="Flat roof"),
    }


def kseb_fees():
    from pricing.tests.factories import StatutoryFeeFactory

    return [
        StatutoryFeeFactory(label="3 KW", capacity_kw_max=Decimal("3"), amount=Decimal("5400.00")),
        StatutoryFeeFactory(label="5 KW", capacity_kw_max=Decimal("5"), amount=Decimal("7800.00")),
        StatutoryFeeFactory(label="8 KW", capacity_kw_max=Decimal("8"), amount=Decimal("11240.00")),
    ]


def _row(sku: str, role: str, quantity: int, attributes: dict) -> dict:
    return {"componentId": sku, "role": role, "quantity": quantity, "treatment": "INCLUDED", "customerFacing": True, "attributes": attributes}


def document(number: str) -> dict:
    rows = [
        _row("i20", "INVERTER", 1, {"brand": "Sungrow", "model": "SG 3.0RT", "capacity": "3 kW", "phase": "1P", "technology": "String Inverter"}),
        _row("p2", "PANEL", 6, {"brand": "Adani", "model": "Adani Mono perc Bifacial 550W DCR", "moduleWatt": 550, "technology": "DCR", "dcr": True}),
        _row("sm_25x15", "STRUCTURE", 3, {"model": "2.5×1.5 Square Tube 16 Gauge GP"}),
        _row("a1", "ACDB", 1, {"brand": "ETN+Mersen", "model": "ETN 32A + Mersen ACDB"}),
    ]
    return {"version": 1, "payload": {"quotation": {"quotationNumber": number}, "bomSummary": {"available": True, "rows": rows}}, "snapshot": {}}


_numbers = iter(range(7000, 10**6))


def issued_version(*, owner=None, customer=None, quotation_status: str = "ISSUED", final_price=Decimal("222000.00")):
    """An ISSUED quotation (and its ISSUED version 1): 3 kW 1P on-grid VALUE, flat roof, 229,000 incl. GST,
    offer 5,000 + approved discounts 2,000 → 222,000."""
    from quotations.models import Quotation, Version

    serial = next(_numbers)
    number = f"GR-{serial}"
    customer = customer or CustomerFactory(name="Test Customer One", phone_e164=f"+9190000{serial:05d}", address="Test Street 1", pincode="688001", district="Alappuzha")
    quotation = Quotation.objects.create(
        customer=customer,
        owner=owner,
        status=quotation_status,
        number=number,
        valid_until=timezone.localdate() + dt.timedelta(days=7),
        issued_at=timezone.now(),
        accepted_at=timezone.now() if quotation_status == "ACCEPTED" else None,
        cancelled_at=timezone.now() if quotation_status == "CANCELLED" else None,
    )
    frozen = document(number)
    version = Version.objects.create(
        quotation=quotation,
        number=1,
        status="ISSUED",
        system_type="ONGRID",
        tier="VALUE",
        size_key="3",
        size_kw=Decimal("3"),
        phase="1P",
        roof_type="FLAT",
        structure_type="flatRoof",
        language="en",
        customer_price_incl_gst=Decimal("229000.00"),
        transport_extra=Decimal("0.00"),
        offer_total=Decimal("5000.00"),
        discount_total=Decimal("2000.00"),
        final_price=final_price,
        document_payload=frozen,
        document_payload_sha256=sha256_hex(frozen),
        issued_at=timezone.now(),
        legacy=True,
    )
    Quotation.objects.filter(pk=quotation.pk).update(current_version=version)
    version.refresh_from_db()
    return version
