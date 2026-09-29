"""Wave-4a integration: the main backend's ``bom_quotationtestimonial`` and ``sent_quotes`` are imported through the
quotations package's ``legacy_import`` (the ``backend.quotations`` step, DV-120) and verified by check #1 (row counts
vs ``core_legacy_map``), #2 (integrity) and #12 (batch audit rows)."""

from __future__ import annotations

import json

import pytest

from accounts.services.seeds import seed_roles
from core.models import LegacyMap
from migrations_tools.services import backend
from migrations_tools.services.http import InProcessClient, unthrottled
from migrations_tools.services.source import TablesSource
from migrations_tools.services.verify import Verifier
from migrations_tools.tests.conftest import BACKEND_FIXTURE, CMS_FIXTURE, load_tables, run
from quotations.models import EmailLog
from quotations.models import Testimonial as HomeownerTestimonial

pytestmark = pytest.mark.django_db
SENT_QUOTES = [
    {
        "id": 1,
        "quote_id": "GR-Q-0001",
        "name": "Customer 01",
        "phone": "9000000001",
        "quote_url": "https://goldenray.in/q/GR-Q-0001",
        "is_sent": True,
        "created_at": "2025-03-01T10:00:00+00:00",
        "updated_at": "2025-03-01T10:02:00+00:00",
    },
    {
        "id": 2,
        "quote_id": "GR-Q-0002",
        "name": "Customer 02",
        "phone": "9000000002",
        "quote_url": "https://goldenray.in/q/GR-Q-0002",
        "is_sent": False,
        "created_at": "2025-03-02T10:00:00+00:00",
        "updated_at": "2025-03-02T10:00:00+00:00",
    },
    {"id": 3, "quote_id": "", "name": "Junk", "phone": "", "quote_url": "", "is_sent": False, "created_at": "2025-03-03T10:00:00+00:00", "updated_at": "2025-03-03T10:00:00+00:00"},
]


@pytest.fixture
def backend_tables():
    tables = load_tables(BACKEND_FIXTURE)
    tables["sent_quotes"] = [dict(row) for row in SENT_QUOTES]  # the UAT dump has none; synthetic, masked rows
    return tables


@pytest.fixture
def imported_with_quotes(db, tmp_path, backend_tables):
    seed_roles()
    path = tmp_path / "backend.json"
    path.write_text(json.dumps({"tables": backend_tables}))
    run("import_cms", "--source-fixture", str(CMS_FIXTURE))
    return run("import_backend", "--source-fixture", str(path)), path


def check(backend_tables, *numbers):
    verifier = Verifier(sources={"BACKEND": TablesSource(backend_tables)}, new=InProcessClient(), offline=True)
    with unthrottled():
        return {result.number: result for result in verifier.run(set(numbers))}


def test_the_backend_plan_imports_both_tables():
    assert {"bom_quotationtestimonial", "sent_quotes"} <= set(backend.PLAN.tables)
    assert not {"bom_quotationtestimonial", "sent_quotes"} & set(backend.PLAN.not_migrated)
    assert "send_quote_junk" in backend.PLAN.not_migrated


def test_testimonials_and_sent_quotes_are_imported_and_mapped(imported_with_quotes, backend_tables):
    out, path = imported_with_quotes
    assert "backend.quotations [imported]" in out
    testimonials = backend_tables["bom_quotationtestimonial"]
    assert HomeownerTestimonial.objects.filter(show_on_website=True).count() == len(testimonials) == 3
    for row in testimonials:
        target = LegacyMap.objects.get(source_system="BACKEND", source_table="bom_quotationtestimonial", source_id=str(row["id"]))
        assert HomeownerTestimonial.objects.get(pk=target.target_id).customer_name == row["name"]
    sent = EmailLog.objects.get(legacy_ref="GR-Q-0001")
    assert sent.channel == "LEGACY_LINK" and sent.status == "SENT" and sent.version_id is None and sent.to == "9000000001"
    assert EmailLog.objects.get(legacy_ref="GR-Q-0002").status == "QUEUED"
    mapped = dict(LegacyMap.objects.filter(source_system="BACKEND", source_table="sent_quotes").values_list("source_id", "target_table"))
    assert mapped == {"1": "quotations_email_log", "2": "quotations_email_log"}  # row 3 has no quote_id: listed, not imported
    again = run("import_backend", "--source-fixture", str(path), "--only", "backend.quotations")
    assert "created 0, updated 0" in again and EmailLog.objects.count() == 2


def test_verify_accounts_for_testimonials_and_sent_quotes(imported_with_quotes, backend_tables):
    outcome = check(backend_tables, 1, 2, 12)
    assert {number: result.status for number, result in outcome.items()} == {1: "pass", 2: "pass", 12: "pass"}, {n: r.details for n, r in outcome.items()}
    LegacyMap.objects.filter(source_system="BACKEND", source_table="sent_quotes", source_id="2").delete()
    LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_quotationtestimonial", source_id="1").delete()
    details = check(backend_tables, 1)[1].details
    assert any("sent_quotes: 1 of 3 rows neither mapped nor listed (2)" in detail for detail in details), details
    assert any("bom_quotationtestimonial: 1 of 3 rows neither mapped nor listed (1)" in detail for detail in details), details


def test_verify_integrity_finds_a_deleted_email_log(imported_with_quotes, backend_tables):
    EmailLog.objects.filter(legacy_ref="GR-Q-0001").delete()
    assert any("quotations_email_log: 1 dangling targets" in detail for detail in check(backend_tables, 2)[2].details)
