"""Legacy import (PLAN §7.3/§7.4) from the committed, masked fixtures — idempotency, violations, and the parity rule for
imported quotations: the frozen document is stored byte for byte (``legacy = true``, no PackRelease), and re-rendering
it hashes to the same payload SHA-256 (nothing is re-derived)."""

from __future__ import annotations

import copy
import json

import pytest

from core.models import LegacyMap
from core.sequences import next_number
from documents.models import RenderJob
from documents.services.jobs import run_job
from engines.frozen import sha256_hex, to_json
from quotations.models import BomSnapshot, CommercialSnapshot, ContentStatus, ContentVersion, EmailLog, Inclusion, Quotation, QuotationStatus
from quotations.models import Testimonial as HomeownerTestimonial
from quotations.models import TierDisplayName, Version, VersionStatus
from quotations.services import legacy_import, lifecycle
from quotations.tests import flarize

pytestmark = pytest.mark.django_db
KEYS = {"created", "updated", "skipped", "violations"}


def _codes(result) -> list[str]:
    return sorted(violation["code"] for violation in result["violations"])


def test_content_masters_are_idempotent():
    inclusions = flarize.fixture("flarize/quotation-inclusions.json")
    first = legacy_import.import_flarize_inclusions(inclusions)
    assert set(first) >= KEYS and first["created"] == Inclusion.objects.count() > 0
    again = legacy_import.import_flarize_inclusions(inclusions)
    assert again["created"] == 0 and again["updated"] == 0 and again["skipped"] == first["created"]
    names = flarize.fixture("flarize/tier-display-names.json")
    assert legacy_import.import_flarize_tier_names(names)["created"] == TierDisplayName.objects.count() == 6
    assert legacy_import.import_flarize_tier_names(names)["created"] == 0
    assert set(TierDisplayName.objects.filter(is_recommended=True).values_list("system_type", "tier")) == {("ONGRID", "VALUE"), ("HYBRID", "VALUE")}
    testimonials = flarize.fixture("flarize/quotation-testimonials.json")
    created = legacy_import.import_flarize_testimonials(testimonials)["created"]
    assert created == len(testimonials["entries"]) == HomeownerTestimonial.objects.count()
    assert legacy_import.import_flarize_testimonials(testimonials)["created"] == 0
    store = flarize.fixture("flarize/quotation-content.json")
    content = legacy_import.import_flarize_content(store)
    assert content["created"] >= 1
    published = ContentVersion.objects.get(status=ContentStatus.PUBLISHED)
    assert published.release_payload["inclusionMatrix"] and published.release_payload["tierDisplayNames"]["ONGRID"]
    assert legacy_import.import_flarize_content(store)["created"] == 0
    assert LegacyMap.objects.filter(source_system=LegacyMap.SourceSystem.FLARIZE, source_table="quotation-inclusions.json").count() == Inclusion.objects.count()


def test_dry_run_writes_nothing():
    result = legacy_import.import_flarize_inclusions(flarize.fixture("flarize/quotation-inclusions.json"), dry_run=True)
    assert result["created"] > 0 and Inclusion.objects.count() == 0
    assert legacy_import.import_flarize_counter({"lastNumber": 50}, dry_run=True)["updated"] == 1
    assert next_number("QUO") == "GR-1"


def test_backend_testimonials():
    rows = flarize.fixture("backend/bom_quotationtestimonial.json")
    result = legacy_import.import_backend_testimonials(rows)
    assert result["created"] == len(rows) == HomeownerTestimonial.objects.filter(show_on_website=True).count()
    assert legacy_import.import_backend_testimonials(rows)["created"] == 0
    row = HomeownerTestimonial.objects.get(customer_name="Homeowner 01")
    assert row.quote_ml and row.system_label == "5 kW System" and str(row.capacity_kw) == "5.00"
    broken = legacy_import.import_backend_testimonials([{"id": 99, "name": "", "quote": "x"}, {"id": 98, "name": "N", "quote": "q", "photo": "t/1.jpg", "bill_before": "100", "bill_after": "900"}])
    assert _codes(broken) == ["incomplete_row", "photo_file_not_migrated"]
    assert HomeownerTestimonial.objects.get(customer_name="N").bill_after is None


def test_branding_is_reported_not_written():
    result = legacy_import.import_flarize_branding(flarize.fixture("flarize/quotation-branding-state.json"))
    assert result["created"] == result["updated"] == 0 and result["skipped"] == len(result["violations"]) > 0
    assert set(_codes(result)) <= {"demo_branding_not_migrated", "enter_as_bank_account"}
    assert all("account" not in violation["message"].lower() or "number" not in violation["message"].lower() for violation in result["violations"])


def test_counter_continues_the_flarize_numbers():
    counter = flarize.fixture("flarize/quotation-counter.json")
    assert legacy_import.import_flarize_counter(counter)["updated"] == 1
    assert legacy_import.import_flarize_counter(counter)["skipped"] == 1
    assert legacy_import.import_flarize_counter({"lastNumber": 5})["skipped"] == 1  # never moves back
    assert _codes(legacy_import.import_flarize_counter({"lastNumber": "x"})) == ["invalid_counter"]
    assert next_number("QUO") == f"GR-{counter['lastNumber'] + 1}"


def test_sent_quotes():
    assert legacy_import.import_sent_quotes(flarize.fixture("backend/sent_quotes.json")) == {"created": 0, "updated": 0, "skipped": 0, "violations": []}
    rows = [
        {
            "id": 1,
            "quote_id": "Q-1",
            "name": "Customer 01",
            "phone": "9000000011",
            "quote_url": "https://example.com/q/1",
            "is_sent": True,
            "created_at": "2025-01-02T10:00:00Z",
            "updated_at": "2025-01-02T10:05:00Z",
        },
        {"id": 2, "quote_id": "Q-2", "name": "Customer 02", "phone": "9000000012", "quote_url": "https://example.com/q/2", "is_sent": False, "created_at": "2025-01-03T10:00:00Z"},
        {"id": 3, "quote_id": "", "name": "x"},
    ]
    first = legacy_import.import_sent_quotes(rows)
    assert (first["created"], _codes(first)) == (2, ["incomplete_row"])
    assert EmailLog.objects.get(legacy_ref="Q-1").status == "SENT" and EmailLog.objects.get(legacy_ref="Q-2").sent_at is None
    assert legacy_import.import_sent_quotes(rows)["skipped"] == 2
    rows[1]["is_sent"] = True
    rows[1]["updated_at"] = "2025-01-04T10:00:00Z"
    assert legacy_import.import_sent_quotes(rows)["updated"] == 1
    assert EmailLog.objects.filter(channel="LEGACY_LINK", version__isnull=True).count() == 2


# ── quotation-state.json ───────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def state():
    return flarize.fixture("flarize/quotation-state.json")


def test_quotations_are_imported_byte_for_byte(state, document_storage):
    result = legacy_import.import_flarize_quotations(state)
    rows = state["quotations"]
    assert result["created"] == len(rows) == Quotation.objects.filter(legacy=True).count() == 7
    codes = _codes(result)
    assert codes.count("orphan_snapshot") == 2 and "unmapped_owner" in codes and "customer_created" in codes
    for qid, record in rows.items():
        quotation = Quotation.objects.get(legacy_ref=qid)
        documents = state["documents"].get(qid) or []
        if not documents:
            assert quotation.status == QuotationStatus.DRAFT and quotation.number == ""
            assert quotation.current_version.status == VersionStatus.DRAFT and quotation.current_version.document_payload is None
            continue
        assert quotation.status == QuotationStatus.ISSUED and quotation.number == record["quotationNumber"]
        version = quotation.current_version
        document = documents[-1]
        assert version.legacy is True and version.pack_release_id is None and version.price_release_id is None
        # byte for byte: the canonical JSON of the stored document is the canonical JSON of the source document
        assert to_json(version.document_payload, sort_keys=True) == to_json(document, sort_keys=True)
        assert json.dumps(version.document_payload, sort_keys=True, ensure_ascii=False) == json.dumps(document, sort_keys=True, ensure_ascii=False)
        assert version.document_payload_sha256 == sha256_hex(document)
        tiers = 1 + len(record.get("alternativeOptions") or [])
        assert BomSnapshot.objects.filter(quotation_version=version).count() == CommercialSnapshot.objects.filter(quotation_version=version).count() == tiers
        assert BomSnapshot.objects.get(quotation_version=version, is_primary=True).record == state["bomSnapshots"][record["bomSnapshotId"]]
        assert CommercialSnapshot.objects.get(quotation_version=version, is_primary=True).record == state["commercialSnapshots"][record["commercialSnapshotId"]]
    # validUntil is an ISO timestamp: its local (Asia/Kolkata) date
    assert str(Quotation.objects.get(legacy_ref="QT-ongrid_5sp_base_20260910120000000l3n6").valid_until) == "2026-10-10"
    # customers matched by phone: one customer per distinct phone
    phones = {record["customer"]["phone"] for record in rows.values()}
    assert Quotation.objects.values("customer").distinct().count() == len(phones)
    # idempotent
    again = legacy_import.import_flarize_quotations(state)
    assert again["created"] == 0 and again["updated"] == 0 and again["skipped"] == 7
    assert Version.objects.count() == 7


def test_rerendering_an_imported_quotation_keeps_its_payload_hash(state, document_storage, head_user):
    """Parity: every imported frozen payload re-renders (en and ml) to a render job whose payload hash is the stored
    ``document_payload_sha256`` — the platform replays the legacy document, never re-derives it."""
    legacy_import.import_flarize_quotations(state)
    issued = Version.objects.filter(legacy=True, status=VersionStatus.ISSUED)
    assert issued.count() == 6
    for version in issued:
        for language in ("en", "ml"):
            job = lifecycle.rerender(version, user=head_user, language=language)
            assert job.payload_sha256 == version.document_payload_sha256, (version.quotation.legacy_ref, language)
            assert job.payload == version.document_payload
            assert run_job(str(job.uid)) == RenderJob.Status.DONE
            job.refresh_from_db()
            assert job.asset_id is not None and job.error == ""


def test_a_changed_source_document_never_overwrites_the_frozen_copy(state):
    legacy_import.import_flarize_quotations(state)
    changed = copy.deepcopy(state)
    qid = next(qid for qid, docs in changed["documents"].items() if docs)
    changed["documents"][qid][0]["payload"]["quotation"]["quotationNumber"] = "TAMPERED"
    before = Version.objects.get(quotation__legacy_ref=qid, number=1).document_payload_sha256
    result = legacy_import.import_flarize_quotations(changed)
    assert "frozen_document_changed" in _codes(result)
    assert Version.objects.get(quotation__legacy_ref=qid, number=1).document_payload_sha256 == before


def test_incomplete_rows_and_missing_snapshots(state):
    broken = copy.deepcopy(state)
    qid, record = next((qid, record) for qid, record in broken["quotations"].items() if record.get("bomSnapshotId"))
    broken["bomSnapshots"].pop(record["bomSnapshotId"])
    broken["quotations"]["bad"] = {"quotationId": "bad"}
    broken["quotations"]["nophone"] = {"quotationId": "nophone", "system": {"systemType": "ongrid"}, "customer": {"customerName": "X"}}
    result = legacy_import.import_flarize_quotations(broken)
    codes = _codes(result)
    assert "incomplete_row" in codes and "snapshot_missing" in codes and "no_customer" in codes
    assert not Quotation.objects.filter(legacy_ref__in=["bad", "nophone"]).exists()


def test_imported_owner_and_customer_through_the_legacy_map(state, make_user):
    from customers.tests.factories import CustomerFactory

    qid, record = next((qid, record) for qid, record in state["quotations"].items() if record.get("salespersonId") == "admin-001")
    owner = make_user(grants={"quotations": ["view"]})
    customer = CustomerFactory()
    LegacyMap.objects.create(source_system=LegacyMap.SourceSystem.FLARIZE, source_table="users.json", source_id="admin-001", target_table="accounts_user", target_id=owner.pk)
    LegacyMap.objects.create(
        source_system=LegacyMap.SourceSystem.FLARIZE, source_table="customers", source_id=record["customer"]["customerId"], target_table="customers_customer", target_id=customer.pk
    )
    legacy_import.import_flarize_quotations(state)
    quotation = Quotation.objects.get(legacy_ref=qid)
    assert quotation.owner_id == owner.pk and quotation.customer_id == customer.pk


# ── integration (wave 4a): a re-run never overwrites a quotation changed on the platform after the import ─────────


def _issued_qid(state) -> str:
    return next(qid for qid, docs in state["documents"].items() if docs and state["quotations"][qid].get("status") == "ISSUED")


def _still_valid(qid: str) -> Quotation:
    import datetime as dt

    from django.utils import timezone

    # a queryset update: not a platform edit (updated_at is left as imported)
    Quotation.objects.filter(legacy_ref=qid).update(valid_until=timezone.localdate() + dt.timedelta(days=30))
    return Quotation.objects.get(legacy_ref=qid)


def _modified(result) -> list[str]:
    return sorted(violation["source_id"] for violation in result["violations"] if violation["code"] == "modified_on_platform")


def test_reimport_keeps_a_quotation_accepted_on_the_platform(state, head_user):
    """Re-running the import after an imported quotation was accepted reset it to ISSUED and kept ``accepted_at``,
    which violated the quotation's CHECK (IntegrityError); the accepted quotation is now left alone and reported."""
    legacy_import.import_flarize_quotations(state)
    qid = _issued_qid(state)
    lifecycle.accept(_still_valid(qid), user=head_user)
    changed = copy.deepcopy(state)
    changed["quotations"][qid]["district"] = "Changed in Flarize"
    result = legacy_import.import_flarize_quotations(changed)
    assert _modified(result) == [qid]
    quotation = Quotation.objects.get(legacy_ref=qid)
    assert quotation.status == QuotationStatus.ACCEPTED and quotation.accepted_at is not None and quotation.district != "Changed in Flarize"
    assert result["created"] == 0 and result["skipped"] == 7


def test_reimport_keeps_a_quotation_cancelled_on_the_platform(state, head_user):
    legacy_import.import_flarize_quotations(state)
    qid = _issued_qid(state)
    lifecycle.cancel(Quotation.objects.get(legacy_ref=qid), user=head_user, reason="Customer went elsewhere")
    result = legacy_import.import_flarize_quotations(state)
    assert _modified(result) == [qid]
    quotation = Quotation.objects.get(legacy_ref=qid)
    assert quotation.status == QuotationStatus.CANCELLED and quotation.cancelled_at is not None


def test_reimport_keeps_a_quotation_revised_on_the_platform(state):
    """A platform version (a revision's draft) marks the quotation as modified even when the quotation row itself was
    not written after the import; the superseded legacy version is not re-issued."""
    from quotations.tests.factories import VersionFactory

    legacy_import.import_flarize_quotations(state)
    qid = _issued_qid(state)
    quotation = Quotation.objects.get(legacy_ref=qid)
    Version.objects.filter(pk=quotation.current_version_id).update(status=VersionStatus.SUPERSEDED)
    imported = quotation.current_version
    VersionFactory(quotation=quotation, number=imported.number + 1)  # not a version the import created
    result = legacy_import.import_flarize_quotations(state)
    assert _modified(result) == [qid]
    assert Version.objects.get(pk=imported.pk).status == VersionStatus.SUPERSEDED


def test_reimport_still_updates_quotations_untouched_on_the_platform(state, head_user):
    legacy_import.import_flarize_quotations(state)
    qid = _issued_qid(state)
    other = next(key for key in state["quotations"] if key != qid)
    lifecycle.cancel(Quotation.objects.get(legacy_ref=qid), user=head_user, reason="Lost")
    changed = copy.deepcopy(state)
    changed["quotations"][other]["district"] = "Thrissur"
    result = legacy_import.import_flarize_quotations(changed)
    assert _modified(result) == [qid] and result["updated"] == 1
    assert Quotation.objects.get(legacy_ref=other).district == "Thrissur"
