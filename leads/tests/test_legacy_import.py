"""leads.services.legacy_import — the legacy lead tables, idempotent and traceable (fixtures exported read-only from a
private copy of the UAT legacy database after the captured form submissions; personal data masked)."""

import copy
import datetime as dt

import pytest
from django.utils.dateparse import parse_datetime

from audit.models import AuditLog
from core.models import LegacyMap
from customers.services.phones import national_digits
from customers.tests.factories import CustomerFactory
from leads.models import AffiliateApplication, CustomerInstallation, Lead, LeadEvent, WarrantyRequest
from leads.services import legacy_import
from leads.tests.conftest import load_fixture

pytestmark = pytest.mark.django_db
CAPTURE_DAY = dt.datetime(2026, 9, 29, tzinfo=dt.UTC)


def tables():
    return {
        "affiliate_application": load_fixture("affiliate_application.json"),
        "warranty_service_request": load_fixture("warranty_service_request.json"),
        "customer_installations": load_fixture("customer_installations_enriched.json"),
        "lead_collection_home": load_fixture("lead_collection_home.json"),
        "solar_installations": load_fixture("solar_installations.json"),
        "solar_installation_new": load_fixture("solar_installation_new.json"),
    }


def _mapped(table, source_id, model):
    return model.all_objects.get(pk=LegacyMap.objects.get(source_system="BACKEND", source_table=table, source_id=str(source_id)).target_id)


def test_every_table_imports(legacy_pincodes):
    data = tables()
    results = legacy_import.import_all(data, today=CAPTURE_DAY)
    assert {table: (result["created"], result["skipped"]) for table, result in results.items()} == {
        "affiliate_application": (2, 0),
        "warranty_service_request": (2, 0),
        "customer_installations": (28, 0),
        "lead_collection_home": (7, 4),
        "solar_installations": (0, 10),
        "solar_installation_new": (0, 18),
    }
    codes = {violation["code"] for violation in results["lead_collection_home"]["violations"]}
    assert codes == {"mirrored_submission"}
    assert all(violation["code"] == "not_an_installation" for violation in results["solar_installation_new"]["violations"])
    assert AuditLog.objects.filter(action="leads.legacy_import").count() == 6
    assert not CustomerInstallation.objects.filter(customer_name__startswith="Solar").exists()


def test_imported_leads_read_back_as_the_legacy_rows(legacy_pincodes):
    legacy_import.import_all(tables(), today=CAPTURE_DAY)
    for row in load_fixture("lead_collection_home.json"):
        if row["source"] in ("referral_partner", "warranty_service"):
            continue  # mirrors (their data is in the affiliate/warranty rows)
        lead = _mapped("lead_collection_home", row["id"], Lead)
        assert (lead.name, national_digits(lead.phone_e164), lead.form.lower(), lead.source_url) == (row["name"], row["phone_number"], row["source"], row["page"])
        assert lead.payload.get("details", {}) == row["details"]
        assert lead.created_at == parse_datetime(row["created_at"]) and lead.status == "NEW" and lead.number.startswith("L-")
        assert LeadEvent.objects.get(lead=lead).event == "IMPORTED"
    group = _mapped("lead_collection_home", 4, Lead)
    assert (group.kind, group.form) == ("GROUP_PURCHASE", "GROUP_PURCHASE")
    affiliate = _mapped("affiliate_application", 1, AffiliateApplication)
    assert (affiliate.profession, affiliate.district, affiliate.email) == ("REAL_ESTATE_AGENT", "Ernakulam", "partner.one@example.com")
    warranty = _mapped("warranty_service_request", 2, WarrantyRequest)
    assert (warranty.issue_type, warranty.description) == ("NET_METERING", "")
    installation = _mapped("customer_installations", 1, CustomerInstallation)
    assert (installation.pincode, installation.district, str(installation.capacity_kw), installation.status, installation.is_showcase) == ("688008", "ALAPPUZHA", "5.000", "COMPLETED", False)
    assert (_mapped("customer_installations", 23, CustomerInstallation).status, _mapped("customer_installations", 24, CustomerInstallation).status) == ("IN_PROGRESS", "PLANNED")


def test_rerun_is_idempotent_and_changes_update(legacy_pincodes):
    data = tables()
    legacy_import.import_all(data, today=CAPTURE_DAY)
    counts = (Lead.objects.count(), AffiliateApplication.objects.count(), WarrantyRequest.objects.count(), CustomerInstallation.objects.count(), LeadEvent.objects.count())
    again = legacy_import.import_all(data, today=CAPTURE_DAY)
    assert all(result["created"] == 0 and result["updated"] == 0 for result in again.values())
    assert (Lead.objects.count(), AffiliateApplication.objects.count(), WarrantyRequest.objects.count(), CustomerInstallation.objects.count(), LeadEvent.objects.count()) == counts
    changed = copy.deepcopy(data["lead_collection_home"])
    changed[0]["name"] = "Renamed"
    result = legacy_import.import_leads(changed, today=CAPTURE_DAY)
    assert (result["created"], result["updated"]) == (0, 1)
    lead = _mapped("lead_collection_home", changed[0]["id"], Lead)
    assert (lead.name, lead.version) == ("Renamed", 2)


def test_stale_enquiries_are_imported_as_lost():
    rows = [{"id": 1, "name": "Old", "phone_number": "9876543210", "source": "footer", "page": "/", "details": {}, "created_at": "2026-01-01T10:00:00Z", "updated_at": "2026-01-01T10:00:00Z"}]
    legacy_import.import_leads(rows, today=CAPTURE_DAY)
    lead = Lead.objects.get()
    assert lead.status == "LOST" and lead.lost_reason == legacy_import.STALE_REASON


def test_quote_request_rows_were_otp_verified_and_customers_are_linked():
    customer = CustomerFactory(phone_e164="+919876543210")
    rows = [{"id": 5, "name": "Q", "phone_number": "9876543210", "source": "quote_request", "page": "/advanced-calculator", "details": {}, "created_at": "2026-09-20T10:00:00Z"}]
    legacy_import.import_leads(rows, today=CAPTURE_DAY)
    lead = Lead.objects.get()
    assert (lead.kind, lead.otp_verified_at, lead.customer) == ("ADVANCED_CALC", lead.created_at, customer)


def test_row_level_violations():
    leads = [
        {"id": 1, "name": "", "phone_number": "12345", "source": "carrier_pigeon", "page": "", "details": "not a dict", "created_at": "2026-09-20T10:00:00Z"},
        {"id": 2, "name": "Mirror", "phone_number": "9876543210", "source": "referral_partner", "page": "/solar-referral-program", "details": {}, "created_at": "2026-09-20T10:00:00Z"},
    ]
    result = legacy_import.import_leads(leads, today=CAPTURE_DAY)
    assert result["created"] == 2
    assert {v["code"] for v in result["violations"]} == {"unknown_source", "unparsable_phone", "invalid_details", "blank_name"}
    broken = _mapped("lead_collection_home", 1, Lead)
    assert (broken.name, broken.phone_e164, broken.form, broken.kind) == ("—", "", "OTHER", "CONTACT")
    assert broken.payload["details"] == {"Details (as recorded)": "not a dict", "Phone (as recorded)": "12345"}
    assert _mapped("lead_collection_home", 2, Lead).kind == "REFERRAL"  # a mirror without its source row is kept
    affiliates = [
        {"id": 1, "full_name": "A", "phone": "9876543210", "email": "a@example.com", "profession": "Astronaut", "district": "Ernakulam", "created_at": "2026-09-20T10:00:00Z"},
        {"id": 2, "full_name": "B", "phone": "123", "email": "b@example.com", "profession": "Other", "district": "Ernakulam", "created_at": "2026-09-20T10:00:00Z"},
        {"id": 3, "full_name": "C", "phone": "9876543211", "email": "c@example.com", "profession": "Other", "district": "Chennai", "created_at": "2026-09-20T10:00:00Z"},
    ]
    result = legacy_import.import_affiliate_applications(affiliates)
    assert result["created"] == 1 and [v["code"] for v in result["violations"]] == ["unknown_profession", "unparsable_phone", "incomplete_row", "incomplete_row"]
    assert AffiliateApplication.objects.get().profession == "OTHER"
    warranty = [
        {"id": 1, "full_name": "W", "phone": "9876543210", "issue_type": "Roof Leak", "description": "", "created_at": "2026-09-20T10:00:00Z"},
        {"id": 2, "full_name": "X", "phone": "", "issue_type": "Other"},
    ]
    result = legacy_import.import_warranty_requests(warranty)
    assert result["created"] == 1 and [v["code"] for v in result["violations"]] == ["unknown_issue_type", "incomplete_row"]
    installs = [
        {"id": 1, "customer_name": "I", "phone_number": "abc", "pincode": "688008", "address": "", "system_size": 3.3, "installation_date": "2025-01-01", "status": "completed"},
        {"id": 2, "pincode": "", "status": "completed"},
    ]
    result = legacy_import.import_customer_installations(installs)
    assert result["created"] == 1 and [v["code"] for v in result["violations"]] == ["unparsable_phone", "incomplete_row"]
    row = CustomerInstallation.objects.get()
    assert (str(row.capacity_kw), row.phone_e164, row.address, row.district) == ("3.300", "", "(phone as recorded: abc)", "")


def test_dry_run_writes_nothing(legacy_pincodes):
    results = legacy_import.import_all(tables(), dry_run=True, today=CAPTURE_DAY)
    assert results["lead_collection_home"]["created"] == 7
    assert not Lead.objects.exists() and not LegacyMap.objects.exists() and not AuditLog.objects.filter(action="leads.legacy_import").exists()
