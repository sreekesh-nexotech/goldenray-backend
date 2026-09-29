"""Quotation factories for tests that need rows without pricing a quotation (lists, scope, lifecycle, timeline).

``issued_quotation`` makes an ISSUED quotation whose single version is a legacy-style frozen document (no PackRelease,
``legacy = true``) — the same shape an imported Flarize quotation has.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import factory
from django.utils import timezone

from customers.tests.factories import CustomerFactory
from engines.frozen import sha256_hex
from quotations.models import Quotation, QuotationStatus, Testimonial, Version, VersionStatus

DOCUMENT = {"version": 1, "payload": {"quotation": {"quotationNumber": "GR-1"}, "pricing": {"customer": {"customerTotalIncludingGST": 229000}}}, "snapshot": {}}


class QuotationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Quotation

    customer = factory.SubFactory(CustomerFactory)
    status = QuotationStatus.DRAFT


class VersionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Version

    quotation = factory.SubFactory(QuotationFactory)
    number = 1
    status = VersionStatus.DRAFT
    system_type = "ONGRID"
    tier = "VALUE"
    size_key = "3"
    size_kw = Decimal("3")
    phase = "1P"
    legacy = True


class TestimonialFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Testimonial

    customer_name = factory.Sequence(lambda n: f"Homeowner {n:02d}")
    location = "Alappuzha"
    quote_en = "Our bill went down."
    show_on_website = True


_numbers = iter(range(9000, 10**6))


def issued_quotation(*, owner=None, customer=None, valid_until: dt.date | None = None, status: str = QuotationStatus.ISSUED, number: str | None = None) -> Quotation:
    quotation = QuotationFactory(
        customer=customer or CustomerFactory(),
        owner=owner,
        status=status,
        number=number or f"GR-{next(_numbers)}",
        valid_until=valid_until or (timezone.localdate() + dt.timedelta(days=7)),
        issued_at=timezone.now(),
        accepted_at=timezone.now() if status == QuotationStatus.ACCEPTED else None,
    )
    version = VersionFactory(
        quotation=quotation,
        status=VersionStatus.ISSUED,
        document_payload=DOCUMENT,
        document_payload_sha256=sha256_hex(DOCUMENT),
        issued_at=timezone.now(),
        final_price=Decimal("229000.00"),
    )
    Quotation.objects.filter(pk=quotation.pk).update(current_version=version)
    quotation.refresh_from_db()
    return quotation
