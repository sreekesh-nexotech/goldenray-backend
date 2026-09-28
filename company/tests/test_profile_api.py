"""company/profile/ (staff) — singleton, typed legacy fields, asset rules, write-only encrypted secret."""

import pytest
from django.db import connection

from audit.models import AuditLog
from company.models import CompanyProfile
from company.services import profile as profiles
from core.models import OutboxEvent
from media.tests import files
from media.tests.factories import MediaAssetFactory, stored_asset

pytestmark = pytest.mark.django_db
URL = "/api/v1/company/profile/"


@pytest.fixture
def admin(make_user):
    return make_user(grants={"company": ["view", "edit"], "media": "*"})


@pytest.fixture
def client(auth_client, admin):
    return auth_client(admin)


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        assert api_client.get(URL).status_code == 401
        assert api_client.patch(URL, {}, format="json").status_code == 401

    def test_view_and_edit_are_separate(self, auth_client, make_user):
        viewer = auth_client(make_user(grants={"company": ["view"]}))
        assert viewer.get(URL).status_code == 200
        assert viewer.patch(URL, {"trade_name": "X"}, format="json").status_code == 403
        assert auth_client(make_user(grants={"media": "*"})).get(URL).status_code == 403


class TestRead:
    def test_first_read_creates_the_singleton_with_legacy_defaults(self, client):
        body = client.get(URL).json()
        assert CompanyProfile.objects.count() == 1
        assert body["quotation_offer_title"] == "Priority 10-Day Installation"
        assert body["quotation_offer_title_ml"] == "10 ദിവസത്തിനുള്ളിൽ മുൻഗണനാ ഇൻസ്റ്റലേഷൻ"
        assert body["quotation_offer_image_src"] == "https://golden-ray.b-cdn.net/icons/37.png" and body["quotation_offer_active"] is True
        assert body["country_code"] == "IN" and body["notify_on_new_lead"] is True and body["careers_accepting_general_applications"] is True
        assert body["blog_revalidate_secret_set"] is False and "blog_revalidate_secret" not in body
        assert client.get(URL).json()["uid"] == body["uid"]
        assert CompanyProfile.objects.count() == 1

    def test_singleton_is_enforced_by_the_database(self):
        from django.db import IntegrityError, transaction

        CompanyProfile.objects.create()
        with pytest.raises(IntegrityError), transaction.atomic():
            CompanyProfile.objects.create()


class TestUpdate:
    def test_fields_and_assets(self, client, admin):
        logo = stored_asset(admin, data=files.png(), name="logo.png")
        signature = stored_asset(admin, data=files.png(), name="sig.png", kind="SIGNATURE", visibility="PRIVATE")
        payload = {
            "legal_name": "Golden Ray Energy Solutions Pvt Ltd",
            "trade_name": "Flarize",
            "gstin": "32aabcg1234f1z5",
            "pan": "AABCG1234F",
            "email": "hello@flarize.com",
            "phone_e164": "+914842000000",
            "address_line": "MG Road",
            "address_locality": "Kochi",
            "address_region": "Kerala",
            "postal_code": "682016",
            "country_code": "in",
            "logo": str(logo.uid),
            "signature": str(signature.uid),
            "trust_stats": [{"label": "Installations", "value": "5000+"}, {"label": "Rating", "value": "4.9", "icon": "star"}],
            "social": {"instagram": "https://instagram.com/flarize"},
            "lead_notification_emails": ["Sales@Flarize.com", "sales@flarize.com", "ops@flarize.com"],
            "notify_on_new_application": False,
            "careers_intro": "Join us",
            "quotation_offer_valid_from": "2026-09-01",
            "quotation_offer_valid_until": "2026-12-31",
            "expected_version": 1,
        }
        client.get(URL)
        response = client.patch(URL, payload, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["gstin"] == "32AABCG1234F1Z5" and body["country_code"] == "IN" and body["version"] == 2
        assert body["logo"]["uid"] == str(logo.uid) and body["logo"]["url"] == logo.cdn_url
        assert body["signature"]["uid"] == str(signature.uid) and body["signature"]["url"] is None
        assert body["lead_notification_emails"] == ["sales@flarize.com", "ops@flarize.com"]
        assert body["trust_stats"][1] == {"label": "Rating", "value": "4.9", "icon": "star"}
        entry = AuditLog.objects.get(action="company.profile_updated")
        assert entry.actor == admin and entry.after["trade_name"] == "Flarize" and entry.after["logo"] == str(logo.uid)
        event = OutboxEvent.objects.get(event_type="company.profile_updated")
        assert "logo" in event.payload["fields"] and event.payload["profile_uid"] == body["uid"]

    @pytest.mark.parametrize(
        "field,asset_kwargs,message",
        [
            ("logo", {"visibility": "PRIVATE", "cdn_url": ""}, "public"),
            ("default_og_image", {"kind": "DOCUMENT", "mime_type": "application/pdf"}, "IMAGE"),
            ("signature", {"kind": "IMAGE"}, "SIGNATURE"),
            ("quotation_offer_image", {"visibility": "PRIVATE", "cdn_url": ""}, "public"),
        ],
    )
    def test_asset_rules(self, client, field, asset_kwargs, message):
        asset = MediaAssetFactory(**asset_kwargs)
        response = client.patch(URL, {field: str(asset.uid)}, format="json")
        assert response.status_code == 400 and message in response.json()["errors"][field][0]

    def test_deleted_or_unknown_asset(self, client):
        gone = MediaAssetFactory()
        gone.soft_delete()
        assert client.patch(URL, {"logo": str(gone.uid)}, format="json").status_code == 400
        assert client.patch(URL, {"logo": "00000000-0000-0000-0000-000000000000"}, format="json").status_code == 400

    def test_clearing_an_asset(self, client):
        logo = MediaAssetFactory()
        client.patch(URL, {"logo": str(logo.uid)}, format="json")
        assert client.patch(URL, {"logo": None}, format="json").json()["logo"] is None

    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"gstin": "NOTAGSTIN"}, "gstin"),
            ({"pan": "123"}, "pan"),
            ({"phone_e164": "0484 200 0000"}, "phone_e164"),
            ({"country_code": "IND"}, "country_code"),
            ({"social": {"myspace": "https://myspace.com/x"}}, "social"),
            ({"social": {"facebook": "http://facebook.com/x"}}, "social"),
            ({"trust_stats": [{"label": "x"}]}, "trust_stats"),
            ({"trust_stats": [{"label": "a", "value": "b"}] * 13}, "trust_stats"),
            ({"lead_notification_emails": ["not-an-email"]}, "lead_notification_emails"),
            ({"quotation_offer_valid_from": "2026-12-31", "quotation_offer_valid_until": "2026-01-01"}, "quotation_offer_valid_until"),
        ],
    )
    def test_validation_envelope(self, client, payload, field):
        response = client.patch(URL, payload, format="json")
        assert response.status_code == 400
        assert set(response.json()) == {"code", "message", "errors", "error_codes"} and field in response.json()["errors"]

    def test_stale_version(self, client):
        client.patch(URL, {"trade_name": "A"}, format="json")
        response = client.patch(URL, {"trade_name": "B", "expected_version": 1}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_no_change_is_not_a_write(self, client):
        version = client.get(URL).json()["version"]
        assert client.patch(URL, {"country_code": "IN"}, format="json").json()["version"] == version
        assert not AuditLog.objects.filter(action="company.profile_updated").exists()


class TestRevalidateSecret:
    def test_write_only_and_encrypted_at_rest(self, client):
        response = client.patch(URL, {"blog_revalidate_url": "https://flarize.com/api/revalidate", "blog_revalidate_secret": "s3cr3t-revalidate-token"}, format="json")
        body = response.json()
        assert body["blog_revalidate_secret_set"] is True and "s3cr3t" not in response.content.decode()
        with connection.cursor() as cursor:
            cursor.execute("SELECT blog_revalidate_secret FROM company_profile")
            raw = cursor.fetchone()[0]
        assert raw.startswith("gAAAA") and "s3cr3t" not in raw
        assert CompanyProfile.objects.get().blog_revalidate_secret == "s3cr3t-revalidate-token"
        entry = AuditLog.objects.get(action="company.profile_updated")
        assert "s3cr3t" not in str(entry.after) and "s3cr3t" not in str(entry.before)
        assert client.patch(URL, {"blog_revalidate_secret": None}, format="json").json()["blog_revalidate_secret_set"] is False


def test_brand_assets_cannot_be_deleted_while_used(client, admin):
    logo = stored_asset(admin, data=files.png(), name="logo.png")
    client.patch(URL, {"logo": str(logo.uid)}, format="json")
    response = client.delete(f"/api/v1/media/{logo.uid}/")
    assert response.status_code == 409 and response.json()["errors"]["references"] == ["company.companyprofile.logo: 1"]


def test_quotation_offer_semantics():
    import datetime as dt

    profile = CompanyProfile(quotation_offer_valid_from=dt.date(2026, 9, 1), quotation_offer_valid_until=dt.date(2026, 9, 30), quotation_offer_title_ml="", quotation_offer_details="Free cleaning")
    assert profiles.offer_is_active(profile, dt.date(2026, 9, 15)) is True
    assert profiles.offer_is_active(profile, dt.date(2026, 8, 31)) is False
    assert profiles.offer_is_active(profile, dt.date(2026, 10, 1)) is False
    offer = profiles.quotation_offer(profile, dt.date(2026, 9, 15))
    assert offer.title_ml == offer.title and offer.details_ml == "Free cleaning"  # Malayalam falls back to English
    profile.quotation_offer_title = "  "
    assert profiles.offer_is_active(profile, dt.date(2026, 9, 15)) is False
    profile.quotation_offer_title, profile.quotation_offer_enabled = "Offer", False
    assert profiles.offer_is_active(profile, dt.date(2026, 9, 15)) is False
