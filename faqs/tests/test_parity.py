"""Parity with the legacy CMS public FAQ list (``GET /api/faqs?page=<route>[&section=]``, golden files captured from the
legacy server) after importing the same rows through the legacy importers.

* read parity: every route that has FAQs, every ``(route, section)`` pair and an unknown route, both datasets, through
  ``?route=`` and the legacy ``?page=`` alias. The only normalisation is PLAN §6.3's: each legacy integer ``id`` is
  replaced by the uid its row was imported as (looked up in ``core_legacy_map``);
* write parity: the operations of ``sitepages/tests/parity/apply_legacy_writes.py`` (reorder with a foreign and an
  unknown id, publish, publish refused, archive, unpublish, restore) through the new staff API reproduce the payloads
  the legacy server served after running them through the legacy services.
"""

import uuid

import pytest

from faqs.models import Faq
from sitepages.models import Page
from sitepages.tests import legacy_fixtures as fx

pytestmark = pytest.mark.django_db
URL = "/api/public/v1/faqs/"


@pytest.fixture(autouse=True)
def site_url(settings):
    settings.FRONTEND_BASE_URL = fx.SITE_URL


def assert_matches_golden(api_client, golden, faq_uids):
    for case in golden:
        query = dict(case["query"])
        route = query.pop("page")
        for params in ({"route": route, **query}, {"page": route, **query}):
            response = api_client.get(URL, params)
            assert response.status_code == case["status"], params
            if case["status"] == 200:
                assert response.json() == fx.with_uids(case["body"], faq_uids), params


@pytest.mark.parametrize("dataset", fx.DATASETS)
def test_faq_list_parity(api_client, dataset):
    fx.import_dataset(dataset)
    faq_uids = fx.legacy_uid_map("faqs_faq")
    assert len(faq_uids) == len(fx.load("faqs", dataset, "faqs_faq"))
    golden = fx.load("faqs", dataset, "golden_faqs")
    assert_matches_golden(api_client, golden, faq_uids)
    assert sum(1 for case in golden if case["status"] == 200) >= 14


def test_faq_write_parity(api_client, auth_client, make_user):
    fx.import_dataset("enriched")
    faq_uids = fx.legacy_uid_map("faqs_faq")
    client = auth_client(make_user(grants={"faqs": "*"}))

    def faq_url(legacy_id, action):
        return f"/api/v1/faqs/{faq_uids[legacy_id]}/{action}/"

    subsidy = Page.objects.get(route="/subsidy")
    order = [faq_uids[31], faq_uids[27], str(uuid.uuid4()), faq_uids[59], faq_uids[25]]  # an unknown and a foreign uid
    reordered = client.post("/api/v1/faqs/reorder/", {"page": str(subsidy.uid), "section": "", "order": order}, format="json")
    assert reordered.status_code == 200
    assert [row["uid"] for row in reordered.json()][:3] == [faq_uids[31], faq_uids[27], faq_uids[25]]
    assert client.post(faq_url(101, "publish")).status_code == 200
    refused = client.post(faq_url(102, "publish"))
    assert refused.status_code == 400 and refused.json()["code"] == "faq_not_publishable" and refused.json()["errors"]["publish"] == ["An answer is required."]
    assert client.post(faq_url(60, "archive")).status_code == 200
    assert client.post(faq_url(10, "unpublish")).status_code == 200
    assert client.post(faq_url(80, "restore")).status_code == 200
    assert Faq.objects.get(uid=faq_uids[80]).status == Faq.Status.DRAFT

    # The page-side operations of the same script (the golden files were captured after all of them).
    pages = auth_client(make_user(grants={"pages": ["view", "publish"]}))
    for route, action in (("/resources", "publish"), ("/terms", "unpublish")):
        assert pages.post(f"/api/v1/pages/{Page.objects.get(route=route).uid}/{action}/").status_code == 200

    assert_matches_golden(api_client, fx.load("faqs", "enriched", "golden_faqs_after_writes"), faq_uids)
