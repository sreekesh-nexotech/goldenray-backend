"""CMS ``siteconfig_settings`` and main-backend ``bom_quotationsettings`` → company profile (DV-11 mapping tables), with
the committed singleton rows of ``migrations_tools/tests/fixtures`` plus filled-in variants."""

import datetime as dt
import json
from pathlib import Path

import pytest

from audit.models import AuditLog
from company.services import legacy_import
from company.services.profile import current_profile
from core.models import LegacyMap
from media.tests.files import jpeg, pdf

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).resolve().parents[2] / "migrations_tools/tests/fixtures"
SITE = json.loads((FIXTURES / "cms.json").read_text())["tables"]["siteconfig_settings"][0]
QUOTATION = json.loads((FIXTURES / "backend.json").read_text())["tables"]["bom_quotationsettings"][0]


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"


def filled(**overrides):
    return {
        **SITE,
        "company_name": "Flarize Solar",
        "company_email": "Hello@Flarize.com",
        "company_phone": "0484 2 123456",
        "address_line": "MG Road",
        "country_code": "in",
        "lead_notification_emails": "sales@flarize.com, SALES@flarize.com; bad-address",
        "application_notification_emails": "hr@flarize.com",
        "notify_on_new_lead": False,
        "careers_intro": "Join us",
        **overrides,
    }


class TestSiteSettings:
    def test_seeded_row_sets_only_the_site_url(self):
        result = legacy_import.import_site_settings([SITE], site_url="http://localhost:3000/")
        profile = current_profile()
        assert result["created"] == 1 and profile.website == "http://localhost:3000" and profile.trade_name == ""
        assert LegacyMap.objects.filter(source_system="CMS", source_table="siteconfig_settings", target_id=profile.pk).exists()
        assert legacy_import.import_site_settings([SITE], site_url="http://localhost:3000/")["skipped"] == 1

    def test_empty_seeded_row_without_site_url_writes_nothing(self):
        result = legacy_import.import_site_settings([SITE])
        assert result["skipped"] == 1 and current_profile().pk is None and not LegacyMap.objects.exists()

    def test_filled_row_maps_every_field(self):
        result = legacy_import.import_site_settings([filled()])
        profile = current_profile()
        assert profile.trade_name == profile.legal_name == "Flarize Solar" and profile.email == "hello@flarize.com"
        assert profile.phone_e164 == "+914842123456" and profile.country_code == "IN" and profile.careers_intro == "Join us"
        assert profile.lead_notification_emails == ["sales@flarize.com"] and profile.application_notification_emails == ["hr@flarize.com"]
        assert profile.notify_on_new_lead is False
        assert [violation["code"] for violation in result["violations"]] == ["email_invalid"]
        assert AuditLog.objects.filter(action="company.legacy_import").count() == 1
        assert AuditLog.objects.filter(action="company.profile_updated").exists()

    def test_bad_values_are_listed_and_empty_texts_never_blank_existing_values(self):
        legacy_import.import_site_settings([filled()])
        result = legacy_import.import_site_settings([filled(company_name="", company_phone="12", country_code="IND", company_email="nope")], site_url="https://flarize.com")
        codes = [violation["code"] for violation in result["violations"]]
        assert codes == ["email_invalid", "phone_invalid", "country_invalid", "email_invalid"]
        profile = current_profile()
        assert profile.trade_name == "Flarize Solar" and profile.phone_e164 == "+914842123456" and profile.website == "https://flarize.com"

    def test_only_the_first_row_counts_and_dry_run_rolls_back(self):
        result = legacy_import.import_site_settings([filled(), {**filled(), "id": 2}], dry_run=True)
        assert result["created"] == 1 and result["violations"][-1]["code"] == "not_singleton"
        assert current_profile().pk is None


class TestQuotationSettings:
    def test_committed_row(self):
        result = legacy_import.import_quotation_settings([QUOTATION])
        profile = current_profile()
        assert result["created"] == 1 and result["violations"] == []
        assert profile.quotation_offer_title == "Priority 10-Day Installation" and profile.quotation_offer_valid_until == dt.date(2026, 12, 31)
        assert profile.quotation_offer_title_ml.startswith("10 ദിവസ") and profile.quotation_offer_enabled
        assert legacy_import.import_quotation_settings([QUOTATION])["skipped"] == 1

    def test_uploaded_offer_image_is_stored_once(self):
        image = jpeg()
        row = {**QUOTATION, "offer_image": "quotation/offers/banner.jpg"}
        legacy_import.import_quotation_settings([row], read_file=lambda path: image)
        asset = current_profile().quotation_offer_image
        assert asset is not None and asset.is_public and asset.folder == "company"
        again = legacy_import.import_quotation_settings([row], read_file=lambda path: image)
        assert again["skipped"] == 1 and current_profile().quotation_offer_image.pk == asset.pk

    def test_offer_image_problems_are_listed(self):
        row = {**QUOTATION, "offer_image": "quotation/offers/banner.jpg"}
        missing = legacy_import.import_quotation_settings([row])
        assert missing["violations"][0]["code"] == "file_unavailable"
        refused = legacy_import.import_quotation_settings([row], read_file=lambda path: pdf())
        assert refused["violations"][0]["code"] == "file_refused"
        pending = legacy_import.import_quotation_settings([row], read_file=lambda path: jpeg(), dry_run=True)
        assert pending["violations"][0]["code"] == "upload_pending"

    def test_invalid_dates_are_listed_not_raised(self):
        result = legacy_import.import_quotation_settings([{**QUOTATION, "offer_valid_from": "2027-01-01", "offer_valid_until": "2026-01-01"}])
        assert result["skipped"] == 1 and result["violations"][0]["code"] == "invalid_value"

    def test_second_row_is_listed(self):
        result = legacy_import.import_quotation_settings([QUOTATION, {**QUOTATION, "id": 2}])
        assert result["violations"][-1]["code"] == "not_singleton"
