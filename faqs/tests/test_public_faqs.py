"""GET /api/public/v1/faqs/?route= — legacy FAQ payload (uid ids), FAQPage schema, section/category, cache, throttle."""

import pytest
from django.core.cache import cache

from faqs.tests.factories import FaqCategoryFactory, FaqFactory, PublishedFaqFactory
from sitepages.tests.factories import PageFactory

pytestmark = pytest.mark.django_db
URL = "/api/public/v1/faqs/"


@pytest.fixture
def page():
    page = PageFactory(route="/subsidy-test", title="Subsidy")
    category = FaqCategoryFactory(name="Subsidy", slug="subsidy")
    PublishedFaqFactory(page=page, question="Second?", answer="Two.", sort_order=1, category=category)
    PublishedFaqFactory(page=page, question="First?", answer="<p>One</p>", sort_order=0)
    PublishedFaqFactory(page=page, question="In a section?", answer="Yes.", section="banks", sort_order=0)
    PublishedFaqFactory(page=page, question="Blank answer?", answer=" ", sort_order=2)
    FaqFactory(page=page, question="Draft?")
    FaqFactory(page=page, question="Archived?", status="ARCHIVED")
    PublishedFaqFactory(page=page, question="Deleted?").soft_delete()
    return page


def test_shape(api_client, page, settings):
    settings.FRONTEND_BASE_URL = "https://flarize.com"
    body = api_client.get(URL, {"route": "/subsidy-test"}).json()
    assert [row["question"] for row in body["data"]] == ["First?", "Second?", "Blank answer?", "In a section?"]
    first = body["data"][0]
    assert set(first) == {"id", "question", "answer", "section", "category", "order"} and len(first["id"]) == 36
    assert (first["answer"], first["section"], first["category"], first["order"]) == ("<p>One</p>", "", None, 0)
    assert body["data"][1]["category"] == "Subsidy"
    assert body["meta"]["page"] == {"name": "Subsidy", "route": "/subsidy-test"} and body["meta"]["count"] == 4
    schema = body["meta"]["schema"]
    assert schema["@type"] == "FAQPage" and schema["url"] == "https://flarize.com/subsidy-test"
    assert [entity["name"] for entity in schema["mainEntity"]] == ["First?", "Second?", "In a section?"]  # a blank answer is left out
    assert schema["mainEntity"][0]["acceptedAnswer"] == {"@type": "Answer", "text": "<p>One</p>"}


def test_section_category_and_legacy_page_alias(api_client, page):
    ask = lambda **params: [row["question"] for row in api_client.get(URL, params).json()["data"]]  # noqa: E731
    assert ask(route="/subsidy-test", section="banks") == ["In a section?"]
    assert ask(route="/subsidy-test", section="") == ["First?", "Second?", "Blank answer?"]
    assert ask(route="/subsidy-test", category="subsidy") == ["Second?"]
    assert ask(route="/subsidy-test", category="nope") == []
    assert api_client.get(URL, {"page": "/subsidy-test"}).json() == api_client.get(URL, {"route": "/subsidy-test"}).json()


def test_empty_list_has_null_schema_and_unpublished_pages_still_serve_their_faqs(api_client):
    draft_page = PageFactory(route="/draft-page", status="DRAFT")
    assert api_client.get(URL, {"route": "/draft-page"}).json() == {"data": [], "meta": {"page": {"name": draft_page.title, "route": "/draft-page"}, "count": 0, "schema": None}}
    PublishedFaqFactory(page=draft_page, question="Still shown?")
    cache.clear()  # the factory writes behind the services' back (no cache bump)
    assert api_client.get(URL, {"route": "/draft-page"}).json()["meta"]["count"] == 1


def test_errors(api_client):
    missing = api_client.get(URL)
    assert missing.status_code == 400 and missing.json()["errors"] == {"route": ["This field is required."]}
    unknown = api_client.get(URL, {"route": "/nope"})
    assert unknown.status_code == 404 and unknown.json()["code"] == "page_not_found"
    PageFactory(route="/gone").soft_delete()
    assert api_client.get(URL, {"route": "/gone"}).status_code == 404


def test_cache_etag_and_invalidation(api_client, auth_client, make_user, page):
    first = api_client.get(URL, {"route": "/subsidy-test"})
    assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS"
    assert api_client.get(URL, {"route": "/subsidy-test"})["X-Cache"] == "HIT"
    assert api_client.get(URL, {"route": "/subsidy-test"}, HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304
    faq = page.faqs.get(question="Draft?")
    auth_client(make_user(grants={"faqs": "*"})).post(f"/api/v1/faqs/{faq.uid}/publish/")
    after = api_client.get(URL, {"route": "/subsidy-test"})
    assert after["X-Cache"] == "MISS" and after.json()["meta"]["count"] == 5
    auth_client(make_user(grants={"pages": "*"})).post(f"/api/v1/pages/{page.uid}/unpublish/")  # page title/route live in meta
    assert api_client.get(URL, {"route": "/subsidy-test"})["X-Cache"] == "MISS"


def test_anonymous_and_throttled_as_public_read(api_client, page, settings):
    from faqs.views.public import PublicFaqListView

    view = PublicFaqListView()
    assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "GET"})()) == "public_read"
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_read": "2/min"}}
    assert [api_client.get(URL, {"route": "/subsidy-test"}).status_code for _ in range(3)] == [200, 200, 429]


def test_bounded(api_client, monkeypatch):
    from faqs.services import delivery

    page = PageFactory(route="/many")
    PublishedFaqFactory.create_batch(3, page=page)
    monkeypatch.setattr(delivery, "PUBLIC_LIMIT", 2)
    assert api_client.get(URL, {"route": "/many"}).json()["meta"]["count"] == 2
