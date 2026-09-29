"""Read parity with the legacy CMS: ``GET /api/page-content?route=`` (golden, captured from the legacy server) ==
``GET /api/public/v1/pages/?route=`` == ``GET /api/public/v1/pages/<slug>/`` after importing the same legacy rows
through ``sitepages.services.legacy_import``. Every route of every page, both datasets; zero differences allowed."""

import pytest

from sitepages.tests import legacy_fixtures as fx

pytestmark = pytest.mark.django_db
URL = "/api/public/v1/pages/"


@pytest.fixture(autouse=True)
def site_url(settings):
    settings.FRONTEND_BASE_URL = fx.SITE_URL


@pytest.mark.parametrize("dataset", fx.DATASETS)
def test_page_content_parity_for_every_route(api_client, dataset):
    fx.import_dataset(dataset, faqs=False)
    golden = fx.load("sitepages", dataset, "golden_page_content")
    slugs = {int(legacy_id): uid for legacy_id, uid in fx.legacy_uid_map("sitepages_page").items()}
    from sitepages.models import Page

    slug_by_route = dict(Page.objects.values_list("route", "slug"))
    assert len(golden) == len(fx.load("sitepages", dataset, "sitepages_page")) + 1 and slugs
    for case in golden:
        route = case["query"]["route"]
        response = api_client.get(URL, {"route": route})
        assert response.status_code == case["status"], route
        if case["status"] == 200:
            assert response.json() == case["body"], route
            by_slug = api_client.get(f"{URL}{slug_by_route[route]}/")
            assert by_slug.status_code == 200 and by_slug.json() == case["body"], route
    # The golden set really exercises the interesting paths.
    if dataset == "enriched":
        bodies = {case["query"]["route"]: case["body"] for case in golden}
        assert bodies["/career"]["data"]["seo"]["schema"]["@type"] == "WebPage"
        assert bodies["/"]["data"]["images"]["promo_banner"] is None and bodies["/"]["data"]["text"]["cta_phone"]
        assert {case["status"] for case in golden} == {200, 404}


def test_write_parity_page_operations(api_client, auth_client, make_user):
    """Page-side operations of ``parity/apply_legacy_writes.py`` through the new staff API reproduce the legacy result."""
    fx.import_dataset("enriched", faqs=False)
    from media.models import MediaAsset
    from sitepages.models import Page

    client = auth_client(make_user(grants={"pages": "*"}))
    career = Page.objects.get(route="/career")
    home = Page.objects.get(route="/")
    subsidy = Page.objects.get(route="/subsidy")
    base = "/api/v1/pages/"
    assert client.patch(f"{base}{career.uid}/text-slots/hero_subtitle/", {"value": "Join a team that installs 200 rooftops a month."}, format="json").status_code == 200
    refused = client.patch(f"{base}{career.uid}/text-slots/hero_title/", {"value": "x" * 91}, format="json")
    assert refused.status_code == 400 and refused.json()["errors"]["value"] == ["This field holds up to 90 characters — you have 91."]
    og = MediaAsset.objects.get(cdn_url__endswith="career-og.webp")
    assert client.patch(f"{base}{home.uid}/image-slots/promo_banner/", {"asset": str(og.uid), "alt": ""}, format="json").status_code == 200
    assert client.patch(f"{base}{career.uid}/image-slots/hero_background/", {"asset": None}, format="json").status_code == 200
    seo = {"meta_description": "Get up to ₹78,000 under PM Surya Ghar — we handle the paperwork.", "schema_type": "WebPage"}
    assert client.patch(f"{base}{subsidy.uid}/seo/", seo, format="json").status_code == 200
    assert client.post(f"{base}{Page.objects.get(route='/resources').uid}/publish/").status_code == 200
    assert client.post(f"{base}{Page.objects.get(route='/terms').uid}/unpublish/").status_code == 200

    for case in fx.load("sitepages", "enriched", "golden_page_content_after_writes"):
        response = api_client.get(URL, {"route": case["query"]["route"]})
        assert response.status_code == case["status"], case["query"]
        if case["status"] == 200:
            assert response.json() == case["body"], case["query"]
