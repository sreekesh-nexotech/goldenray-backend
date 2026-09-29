"""Service-level rules and every DomainError not reached through the API tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from agreements.models import Agreement, AgreementStatus
from agreements.services import acceptance, agreements, document, fees, issuing, pinning
from agreements.tests.factories import AgreementFactory, issued_version
from core.errors import Conflict, DomainError, PermissionDenied


def test_choose_the_smallest_covering_band_phase_first():
    candidates = [
        {"kind": "KSEB_REGISTRATION", "label": "3 KW", "phase": None, "capacity_kw_max": "3", "amount": "5400"},
        {"kind": "KSEB_REGISTRATION", "label": "5 KW", "phase": None, "capacity_kw_max": "5", "amount": "7800"},
        {"kind": "KSEB_REGISTRATION", "label": "5 KW 3P", "phase": "3P", "capacity_kw_max": "5", "amount": "8000"},
        {"kind": "KSEB_REGISTRATION", "label": "any", "phase": None, "capacity_kw_max": None, "amount": "25000"},
        {"kind": "KSEB_METER", "label": "meter", "phase": None, "capacity_kw_max": "10", "amount": "1"},
    ]
    assert fees.choose(candidates, phase="1P", capacity_kw=Decimal("3"))["label"] == "3 KW"
    assert fees.choose(candidates, phase="1P", capacity_kw=Decimal("4"))["label"] == "5 KW"
    assert fees.choose(candidates, phase="3P", capacity_kw=Decimal("4"))["label"] == "5 KW 3P"
    assert fees.choose(candidates, phase="1P", capacity_kw=Decimal("40"))["label"] == "any"
    assert fees.choose(candidates[:1], phase="1P", capacity_kw=Decimal("40")) is None
    assert fees.choose(candidates, phase="1P", capacity_kw=None) is None


def test_fee_from_a_price_release_links_the_live_row(world):
    from types import SimpleNamespace

    release = SimpleNamespace(payload={"statutory_fees": [{"kind": "KSEB_REGISTRATION", "label": "5 KW", "phase": None, "capacity_kw_max": "5.00", "amount": "7800.00"}]})
    fee = fees.from_release(release, phase="1P", capacity_kw=Decimal("4.5"))
    assert fee.amount == Decimal("7800.00") and fee.row_id == world["fees"][1].pk
    stale = SimpleNamespace(payload={"statutory_fees": [{"kind": "KSEB_REGISTRATION", "label": "5 KW", "phase": None, "capacity_kw_max": "5.00", "amount": "7000.00"}]})
    assert fees.from_release(stale, phase="1P", capacity_kw=Decimal("5")).row_id is None
    assert fees.from_release(None, phase="1P", capacity_kw=Decimal("5")) is None
    assert fees.by_amount(Decimal("11240.00")) == world["fees"][2] and fees.by_amount(None) is None


@pytest.mark.django_db
def test_pinning_without_equipment_or_prices_leaves_blanks():
    version = issued_version(final_price=None)
    type(version).objects.filter(pk=version.pk).update(document_payload={"payload": {}}, customer_price_incl_gst=None, final_price=None)
    version.refresh_from_db()
    values = pinning.from_quotation_version(version)
    assert values["panel"] is None and values["panel_label"] == "" and values["inverter_type"] == "" and values["structure_template"] is None
    assert values["original_price"] is None and values["final_price"] is None and values["statutory_fee_amount"] is None


def test_pinning_micro_inverters_and_hybrid(world):
    version = issued_version()
    rows = version.document_payload["payload"]["bomSummary"]["rows"]
    rows[0]["role"] = "MICRO_INVERTER"
    rows[0]["quantity"] = 6
    type(version).objects.filter(pk=version.pk).update(document_payload=version.document_payload, final_price=None, system_type="HYBRID")
    version.refresh_from_db()
    values = pinning.from_quotation_version(version)
    assert values["inverter_type"] == "MICRO" and values["inverter_qty"] == 6 and values["system_type"] == "HYBRID"
    assert values["final_price"] == Decimal("222000.00") and values["original_price"] == Decimal("229000.00")
    payload = version.document_payload
    payload["payload"]["bomSummary"]["rows"][0]["role"] = "INVERTER"
    type(version).objects.filter(pk=version.pk).update(document_payload=payload)
    version.refresh_from_db()
    assert pinning.from_quotation_version(version)["inverter_type"] == "HYBRID"


def test_from_quotation_needs_an_issued_version_of_an_open_quotation(head_user, world):
    draft_quotation = issued_version()
    type(draft_quotation).objects.filter(pk=draft_quotation.pk).update(status="SUPERSEDED")
    with pytest.raises(Conflict) as error:
        agreements.create_from_quotation(user=head_user, data={"quotation_version_uid": draft_quotation.uid})
    assert error.value.code == "quotation_version_not_issued"
    cancelled = issued_version(quotation_status="CANCELLED")
    with pytest.raises(Conflict) as error:
        agreements.create_from_quotation(user=head_user, data={"quotation_version_uid": cancelled.uid})
    assert error.value.code == "quotation_closed"


def test_supersede_repins_to_another_version_of_the_customer(head_user, version, company, document_storage):
    issued = issuing.issue(agreements.create_from_quotation(user=head_user, data={"quotation_version_uid": version.uid}), user=head_user)
    other_customer = issued_version()
    with pytest.raises(DomainError) as error:
        agreements.supersede(issued, user=head_user, data={"quotation_version_uid": other_customer.uid})
    assert "quotation_version_uid" in error.value.errors
    newer = issued_version(customer=version.quotation.customer, final_price=Decimal("220000.00"))
    revision = agreements.supersede(issued, user=head_user, data={"quotation_version_uid": newer.uid})
    assert revision.quotation_version == newer and revision.final_price == Decimal("220000.00") and revision.discount == Decimal("7000.00")


def test_supersede_of_a_legacy_agreement_repairs_its_arithmetic(head_user, world, company, document_storage):
    legacy = AgreementFactory(
        kind="PURCHASE_AGREEMENT",
        status=AgreementStatus.ISSUED,
        number="CRS-agr_1",
        legacy=True,
        original_price=Decimal("100"),
        discount=Decimal("500"),
        final_price=Decimal("7"),
        payload={"legacy_record": {}},
        payload_sha256="0" * 64,
        issued_at=world["panel"].created_at,
    )
    revision = agreements.supersede(legacy, user=head_user)
    assert revision.legacy is False and revision.discount == Decimal("100.00") and revision.final_price == Decimal("0.00")


def test_blank_reprice_refuses_a_negative_price(head_user, world):
    agreement = AgreementFactory(discount=Decimal("0"))
    Agreement.objects.filter(pk=agreement.pk).update(discount=Decimal("500.00"), final_price=Decimal("299500.00"))
    agreement.refresh_from_db()
    with pytest.raises(DomainError) as error:
        agreements.update_draft(agreement, user=head_user, data={"original_price": Decimal("100")})
    assert "original_price" in error.value.errors
    with pytest.raises(DomainError) as error:
        agreements.update_draft(agreement, user=head_user, data={"kind": "SALE_ORDER", "lines": []})
    assert error.value.code == "validation_error"


def test_price_override_guards(head_user, make_user, world, price_override, settings):
    agreement = AgreementFactory()
    with pytest.raises(PermissionDenied):
        acceptance.override_price(agreement, user=make_user(grants={"agreements": ["view", "edit"]}), reason="x", discount=Decimal("1"))
    no_price = AgreementFactory(original_price=None, final_price=None)
    with pytest.raises(Conflict) as error:
        acceptance.override_price(no_price, user=head_user, reason="x", discount=Decimal("1"))
    assert error.value.code == "price_missing"
    from core.models import FeatureFlag
    from flarize.cache_utils import bump

    FeatureFlag.objects.filter(key="AGREEMENTS_PRICE_OVERRIDE").update(enabled=False)
    bump("core:flags")
    with pytest.raises(Conflict) as error:
        acceptance.override_price(agreement, user=head_user, reason="x", discount=Decimal("1"))
    assert error.value.code == "price_override_disabled"


def test_plant_description_follows_the_legacy_rule(world):
    agreement = AgreementFactory(size_label="5KW Plant with 6KW Inverter – Hybrid", phase="3P")
    assert document.is_hybrid(agreement) and document.plant_description(agreement) == "5KW Plant with 6KW Inverter – Hybrid (3 Phase)"
    agreement = AgreementFactory(size_label="8 KW", phase="", inverter_type="HYBRID")
    assert document.plant_description(agreement) == "8 KW Hybrid Solar Power Plant"
    agreement = AgreementFactory(size_label="3 KW", phase="1P")
    assert document.plant_description(agreement) == "3 KW (Single Phase) On-Grid Solar Power Plant"


def test_document_job_lookup(head_user, world, company, document_storage):
    agreement = AgreementFactory()
    with pytest.raises(Conflict):
        issuing.document_job(agreement, "en")
    job = issuing.render(agreement, user=head_user, language="en")
    assert issuing.document_job(agreement, "en") == job
    issued = issuing.issue(agreement, user=head_user)
    assert issuing.document_job(issued, "en") == issued.document_job != job


def test_registrations(head_user, make_user, world, company, document_storage):
    from catalog.services.usage import usage_of
    from core.dashboard import counters_for
    from customers.services import timeline
    from customers.services.merge import blocking_references

    agreement = AgreementFactory(panel=world["panel"])
    AgreementFactory()
    issued = issuing.issue(agreement, user=head_user)
    assert blocking_references(issued.customer)
    entries, _ = timeline.build(issued.customer, user=head_user)
    assert any(entry.kind == "agreements.issued" for entry in entries)
    section = next(section for section in usage_of(world["panel"]) if section.name == "agreements.issued")
    assert section.count == 1 and section.references[0]["object_uid"] == str(issued.uid)
    assert counters_for(head_user)["agreements"] == {"drafts": 1, "issued": 1, "accepted": 0}
    assert "agreements" not in counters_for(make_user(grants={"customers": ["view"]}))
    agreements.cancel(issued, user=head_user, reason="x")
    entries, _ = timeline.build(issued.customer, user=head_user)
    assert any(entry.kind == "agreements.cancelled" for entry in entries)
    from documents.access import ensure_can_view

    outsider = make_user(grants={"agreements": ["view"]}, scopes={"agreements": "owned"})
    from core.errors import NotFound

    with pytest.raises(NotFound):
        ensure_can_view(outsider, issued.document_job)
    ensure_can_view(head_user, issued.document_job)


def test_the_document_prints_the_issue_date_in_india(world, company):
    """Review: the document printed the UTC date of ``issued_at`` — an agreement issued at 01:30 IST was dated the
    previous day."""
    import datetime as dt

    from django.template.loader import get_template

    agreement = AgreementFactory.build(number="AGR-2026-27-0009", issued_at=dt.datetime(2026, 9, 29, 20, 0, tzinfo=dt.UTC))
    payload = document.build(agreement)
    assert payload["agreement"]["issued_on"] == "2026-09-30"
    html = get_template("documents/agreement/en.html").render({"payload": payload, "language": "en"})
    assert "2026-09-30" in html and "2026-09-29" not in html


def _signed_scan():
    from django.core.files.uploadedfile import SimpleUploadedFile

    from media.tests import files

    return SimpleUploadedFile("signed.pdf", files.pdf(), content_type="application/pdf")


def test_a_repinned_revision_cannot_become_a_second_agreement_in_force(head_user, version, company, document_storage):
    """Review: re-pinning a revision onto a version whose Purchase Agreement is already ACCEPTED issued a second
    agreement in force for that version (the partial unique covered ISSUED only)."""
    first = issuing.issue(agreements.create_from_quotation(user=head_user, data={"quotation_version_uid": version.uid}), user=head_user)
    other = issued_version(customer=version.quotation.customer)
    accepted = issuing.issue(agreements.create_from_quotation(user=head_user, data={"quotation_version_uid": other.uid}), user=head_user)
    accepted = acceptance.record_acceptance(accepted, user=head_user, file=_signed_scan())
    assert accepted.status == AgreementStatus.ACCEPTED
    revision = agreements.supersede(first, user=head_user, data={"quotation_version_uid": other.uid})
    with pytest.raises(Conflict) as error:
        issuing.issue(revision, user=head_user)
    assert error.value.code == "agreement_exists"
    first.refresh_from_db()
    assert first.status == AgreementStatus.ISSUED
    assert Agreement.objects.filter(quotation_version=other, kind="PURCHASE_AGREEMENT", status__in=["ISSUED", "ACCEPTED"]).count() == 1


def test_a_revision_of_a_legacy_agreement_keeps_its_quotation_number(head_user, world, company, document_storage):
    """Review: the revision of an imported agreement lost the typed quotation number (its QUOTATION NO row)."""
    legacy = AgreementFactory(
        kind="PURCHASE_AGREEMENT",
        status=AgreementStatus.ISSUED,
        number="CRS-agr_2",
        legacy=True,
        legacy_quotation_ref="QUO-GR-AS-26-1024",
        structure_material="GI",
        payload={"legacy_record": {}},
        payload_sha256="0" * 64,
        issued_at=world["panel"].created_at,
    )
    legacy.customer.address = "Test Street 1"
    legacy.customer.phone_e164 = "+919000000123"
    legacy.customer.save()
    revision = agreements.supersede(legacy, user=head_user)
    assert revision.legacy_quotation_ref == "QUO-GR-AS-26-1024"
    issued = issuing.issue(revision, user=head_user)
    assert issued.payload["quotation"]["number"] == "QUO-GR-AS-26-1024"
