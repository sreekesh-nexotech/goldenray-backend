"""GET /api/public/v1/pages/<slug>/ and /pages/?route= — legacy page-content payload, cache, throttle."""

import pytest

from company.tests.factories import CompanyProfileFactory
from media.tests.factories import MediaAssetFactory
from sitepages.tests.factories import PageFactory, PageImageSlotFactory, PageSeoFactory, PageTextSlotFactory

pytestmark = pytest.mark.django_db
URL = "/api/public/v1/pages/"


@pytest.fixture
def page():
    page = PageFactory(slug="career-test", route="/career-test", title="Careers")
    PageTextSlotFactory(page=page, key="hero_title", value="Join us")
    PageTextSlotFactory(page=page, key="hero_subtitle", value="")
    PageImageSlotFactory(page=page, key="hero_background", asset=MediaAssetFactory(alternative_text="Rooftop", width=1920, height=1080))
    PageImageSlotFactory(page=page, key="unset")
    PageImageSlotFactory(page=page, key="external", external_url="https://golden-ray.b-cdn.net/x.png", alt="External")
    return page


def test_shape_by_slug_and_by_route(api_client, page):
    body = api_client.get(f"{URL}career-test/").json()
    assert body == api_client.get(URL, {"route": "/career-test"}).json()
    data = body["data"]
    assert set(body) == {"data"} and set(data) == {"route", "name", "images", "text", "seo"}
    assert (data["route"], data["name"], data["seo"]) == ("/career-test", "Careers", None)
    assert data["text"] == {"hero_title": "Join us"}  # empty values are omitted: the page keeps its built-in text
    hero = data["images"]["hero_background"]
    assert hero["url"].startswith("https://") and hero["alt"] == "Rooftop" and (hero["width"], hero["height"]) == (1920, 1080)
    assert data["images"]["unset"] is None
    assert data["images"]["external"] == {"url": "https://golden-ray.b-cdn.net/x.png", "alt": "External", "width": None, "height": None}


def test_seo_block_and_webpage_schema(api_client, page, settings):
    settings.FRONTEND_BASE_URL = "https://flarize.com"
    CompanyProfileFactory(trade_name="Flarize")
    og = MediaAssetFactory(alternative_text="Share")
    PageSeoFactory(
        page=page,
        seo_title="Careers at Flarize",
        meta_description="Jobs",
        canonical_url="https://flarize.com/career",
        og_image=og,
        schema_type="WebPage",
        schema_extra={"inLanguage": "en-IN", "name": "ignored"},
    )
    seo = api_client.get(f"{URL}career-test/").json()["data"]["seo"]
    assert seo["title"] == "Careers at Flarize" and seo["noindex"] is False and seo["og_image"]["alt"] == "Share"
    assert seo["schema"] == {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": "Careers at Flarize",
        "description": "Jobs",
        "url": "https://flarize.com/career-test",
        "isPartOf": {"@type": "WebSite", "name": "Flarize", "url": "https://flarize.com"},
        "inLanguage": "en-IN",
    }


def test_non_webpage_schema_is_null_and_unusable_assets_are_null(api_client, page):
    private = MediaAssetFactory(visibility="PRIVATE", cdn_url="")
    PageSeoFactory(page=page, schema_type="FAQPage", og_image=private)
    slot = page.image_slots.get(key="hero_background")
    slot.asset.soft_delete()
    data = api_client.get(f"{URL}career-test/").json()["data"]
    assert data["seo"]["schema"] is None and data["seo"]["og_image"] is None and data["images"]["hero_background"] is None


@pytest.mark.parametrize("status", ["DRAFT", "ARCHIVED"])
def test_only_published_pages_resolve(api_client, status):
    PageFactory(slug="hidden", route="/hidden", status=status)
    for response in (api_client.get(f"{URL}hidden/"), api_client.get(URL, {"route": "/hidden"})):
        assert response.status_code == 404 and response.json()["code"] == "page_not_found"
    assert api_client.get(f"{URL}nope/").status_code == 404


def test_route_is_required(api_client):
    response = api_client.get(URL)
    assert response.status_code == 400 and response.json()["errors"] == {"route": ["This field is required."]}


def test_caching_headers_etag_and_304(api_client, page):
    first = api_client.get(f"{URL}career-test/")
    assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS" and first["ETag"]
    second = api_client.get(f"{URL}career-test/")
    assert second["X-Cache"] == "HIT" and second.json() == first.json()
    not_modified = api_client.get(f"{URL}career-test/", HTTP_IF_NONE_MATCH=first["ETag"])
    assert not_modified.status_code == 304


def test_edits_invalidate_the_cache(api_client, auth_client, make_user, page):
    before = api_client.get(f"{URL}career-test/")
    auth_client(make_user(grants={"pages": "*"})).patch(f"/api/v1/pages/{page.uid}/text-slots/hero_title/", {"value": "Work with us"}, format="json")
    after = api_client.get(f"{URL}career-test/")
    assert after["X-Cache"] == "MISS" and after.json()["data"]["text"]["hero_title"] == "Work with us" and after["ETag"] != before["ETag"]
    asset = page.image_slots.get(key="hero_background").asset
    auth_client(make_user(grants={"media": "*"})).patch(f"/api/v1/media/{asset.uid}/", {"alternative_text": "New alt"}, format="json")
    assert api_client.get(f"{URL}career-test/").json()["data"]["images"]["hero_background"]["alt"] == "New alt"


def test_anonymous_and_throttled_as_public_read(api_client, page, settings):
    from sitepages.views.public import PublicPageByRouteView, PublicPageView

    for view_class in (PublicPageView, PublicPageByRouteView):
        view = view_class()
        assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "GET"})()) == "public_read"
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_read": "2/min"}}
    assert [api_client.get(f"{URL}career-test/").status_code for _ in range(3)] == [200, 200, 429]


def test_bearer_token_is_ignored(api_client, page):
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
    assert api_client.get(f"{URL}career-test/").status_code == 200


def test_malformed_query_values_are_400_not_500(api_client, page):
    """Anonymous input the database cannot compare (a NUL byte) is a validation error, never a server error (§17 #1)."""
    response = api_client.get(URL, {"route": "/career-test\x00"})
    assert response.status_code == 400 and response.json()["code"] == "validation_error" and "route" in response.json()["errors"]
    assert api_client.get(URL, {"route": "/" + "x" * 300}).status_code == 400
    assert api_client.get(URL, {"route": " /career-test"}).status_code == 404  # validated without trimming: another route
