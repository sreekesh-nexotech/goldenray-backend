"""Old CMS delivery through the shim (``/legacy/studio-api/api/…``), replaying the goldens captured from the legacy CMS
(committed by the content-blog, content-pages and careers packages) over the same imported rows: the bodies must be
the legacy ones — integer ids included — and the 404s the CMS ``{"detail": …}`` bodies."""

import json
from urllib.parse import urlencode

import pytest

from blog.services import legacy_import as blog_import
from blog.tests.test_delivery_parity import MANIFEST, TABLES, golden
from careers.tests.test_public_parity import load as load_careers
from careers.tests.test_public_parity import seed as seed_careers
from legacy.tests.conftest import ordered
from sitepages.tests import legacy_fixtures as fx

pytestmark = pytest.mark.django_db
CMS = "/legacy/studio-api/api"


# ── collections ───────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def articles():
    return blog_import.import_all(TABLES)


@pytest.mark.parametrize("case", MANIFEST, ids=[case["name"] for case in MANIFEST])
def test_articles_are_byte_identical(api_client, articles, case):
    url = f"{CMS}/articles" + (f"?{case['query']}" if case["query"] else "")
    response = api_client.get(url)
    if "][$in][" in case["query"]:
        pytest.skip("indexed $in: covered by test_indexed_filters_are_ignored_like_the_cms")
    assert response.status_code == case["status"] and response.content == golden(case["name"])


def test_indexed_filters_are_ignored_like_the_cms(api_client, articles):
    everything = ordered(api_client.get(f"{CMS}/articles"))
    indexed = ordered(api_client.get(f"{CMS}/articles?filters[slug][$in][0]=battery-sizing-guide"))
    assert indexed == everything and indexed["meta"]["pagination"]["total"] == 5


def test_trailing_slash_collection_and_the_cache(api_client, articles):
    first = api_client.get(f"{CMS}/articles/?populate=*")
    assert first.status_code == 200 and first["X-Cache"] == "MISS"
    assert api_client.get(f"{CMS}/articles/?populate=*")["X-Cache"] == "HIT"
    assert first.content == api_client.get(f"{CMS}/articles?populate=*").content


@pytest.mark.parametrize("path", ["case-studies", "authors", "faqs/", "job-positions/", "page-content/", "Articles"])
def test_unknown_collections_are_the_cms_404(api_client, articles, path):
    response = api_client.get(f"{CMS}/{path}")
    assert response.status_code == 404 and ordered(response) == {"detail": f"Unknown collection '{path.rstrip('/')}'"}


def test_a_bad_filter_value_is_a_400_detail(api_client, articles):
    response = api_client.get(f"{CMS}/articles?filters[isFeatured][$eq]=maybe")
    assert response.status_code == 400 and list(response.json()) == ["detail"]


# ── FAQs and page content (legacy integer ids) ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("dataset", fx.DATASETS)
def test_faqs_are_the_legacy_payload(api_client, dataset, django_assert_max_num_queries):
    fx.import_dataset(dataset)
    for case in fx.load("faqs", dataset, "golden_faqs"):
        with django_assert_max_num_queries(8):
            response = api_client.get(f"{CMS}/faqs?{urlencode(case['query'])}")
        assert response.status_code == case["status"], case["query"]
        if case["status"] == 200:
            assert ordered(response) == case["body"], case["query"]
        else:
            assert ordered(response) == {"detail": f"Unknown page '{case['query']['page']}'"}


def test_faqs_without_page_is_the_cms_404(api_client):
    response = api_client.get(f"{CMS}/faqs")
    assert response.status_code == 404 and response.json() == {"detail": "A 'page' query parameter is required."}


@pytest.mark.parametrize("dataset", fx.DATASETS)
def test_page_content_is_the_legacy_payload(api_client, dataset):
    fx.import_dataset(dataset, faqs=False)
    for case in fx.load("sitepages", dataset, "golden_page_content"):
        response = api_client.get(f"{CMS}/page-content?{urlencode(case['query'])}")
        assert response.status_code == case["status"], case["query"]
        if case["status"] == 200:
            assert ordered(response) == case["body"], case["query"]
        else:
            assert ordered(response) == {"detail": f"Unknown page '{case['query']['route']}'"}


def test_page_content_without_route_is_the_cms_404(api_client):
    assert api_client.get(f"{CMS}/page-content?page=/").json() == {"detail": "A 'route' query parameter is required."}


# ── job positions ─────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", ["shared", "enriched"])
def test_job_positions_are_the_legacy_payload(api_client, scenario):
    recorded = load_careers(scenario)
    seed_careers(recorded)
    for key, legacy in recorded["responses"].items():
        if key.startswith("list"):
            query = key.split("?", 1)[1] if "?" in key else ""
            response = api_client.get(f"{CMS}/job-positions" + (f"?{query}" if query else ""))
        else:
            response = api_client.get(f"{CMS}/job-positions/{key.split('/', 1)[1]}")
        assert response.status_code == legacy["status"], key
        assert ordered(response) == (legacy["body"] if legacy["status"] == 200 else {"detail": "This position does not exist."}), key


def test_job_position_with_a_trailing_slash_is_not_routed(api_client):
    assert api_client.get(f"{CMS}/job-positions/some-slug/").status_code == 404


def test_goldens_are_present():
    assert MANIFEST and json.loads(golden(MANIFEST[0]["name"]))
