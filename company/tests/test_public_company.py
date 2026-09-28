"""GET /api/public/v1/company/ — public fields only, version-keyed cache, ETag/304, invalidation on edit."""

import pytest

from company.models import CompanyProfile
from company.tests.factories import BankAccountFactory, CompanyProfileFactory
from media.tests.factories import MediaAssetFactory

pytestmark = pytest.mark.django_db
URL = "/api/public/v1/company/"


def test_shape_and_public_fields_only(api_client):
    logo = MediaAssetFactory(alternative_text="Flarize logo")
    CompanyProfileFactory(logo=logo, pan="AABCG1234F", lead_notification_emails=["sales@flarize.com"], blog_revalidate_secret="shh-secret", social={"instagram": "https://instagram.com/flarize"})
    BankAccountFactory(account_number="123456789012")
    response = api_client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "name",
        "legal_name",
        "trade_name",
        "email",
        "phone",
        "website",
        "address",
        "logo",
        "default_og_image",
        "default_meta_description",
        "trust_stats",
        "social",
        "careers",
        "quotation",
    }
    assert body["name"] == "Flarize" and body["phone"] == "+914842000000"
    assert body["address"] == {"line": "2nd Floor, MG Road", "locality": "Kochi", "region": "Kerala", "postal_code": "682016", "country_code": "IN"}
    assert body["logo"] == {"url": logo.cdn_url, "alternative_text": "Flarize logo", "width": 64, "height": 48}
    assert body["quotation"]["offer"]["active"] is True and body["quotation"]["offer"]["image_src"].startswith("https://")
    text = response.content.decode()
    for secret in ("32AABCG1234F1Z5", "AABCG1234F", "sales@flarize.com", "shh-secret", "123456789012", "uid"):
        assert secret not in text


def test_defaults_without_a_profile_and_no_write(api_client):
    body = api_client.get(URL).json()
    assert body["name"] == "" and body["quotation"]["offer"]["title"] == "Priority 10-Day Installation"
    assert not CompanyProfile.objects.exists()


def test_private_assets_are_never_exposed(api_client):
    CompanyProfileFactory(logo=MediaAssetFactory(visibility="PRIVATE", cdn_url=""))
    assert api_client.get(URL).json()["logo"] is None


def test_caching_headers_etag_and_304(api_client):
    CompanyProfileFactory()
    first = api_client.get(URL)
    assert first["Cache-Control"] == "public, max-age=300" and first["X-Cache"] == "MISS" and first["ETag"]
    second = api_client.get(URL)
    assert second["X-Cache"] == "HIT" and second.json() == first.json()
    not_modified = api_client.get(URL, HTTP_IF_NONE_MATCH=first["ETag"])
    assert not_modified.status_code == 304 and not_modified["ETag"] == first["ETag"]


def test_staff_edit_invalidates_the_cache(api_client, auth_client, make_user):
    CompanyProfileFactory()
    before = api_client.get(URL)
    auth_client(make_user(grants={"company": ["view", "edit"]})).patch("/api/v1/company/profile/", {"trade_name": "Flarize Solar"}, format="json")
    after = api_client.get(URL)
    assert after["X-Cache"] == "MISS" and after.json()["name"] == "Flarize Solar" and after["ETag"] != before["ETag"]


def test_anonymous_and_throttled_as_public_read():
    from company.views.profile import PublicCompanyView

    view = PublicCompanyView()
    assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "GET"})()) == "public_read"


def test_public_throttle_applies(api_client, settings):
    CompanyProfileFactory()
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_read": "2/min"}}
    assert [api_client.get(URL).status_code for _ in range(3)] == [200, 200, 429]
