"""Service-level DomainErrors not reachable through the API (races, rows deleted behind the view's back), scope, cost."""

import pytest
from django.core.cache import cache

from core.errors import DomainError, NotFound
from faqs.services import faqs as faq_services
from faqs.tests.factories import FaqCategoryFactory, FaqFactory, PublishedFaqFactory
from media.tests.factories import MediaAssetFactory
from sitepages.services import content, pages
from sitepages.tests.factories import PageFactory, PageImageSlotFactory, PageTextSlotFactory

pytestmark = pytest.mark.django_db


def test_rows_deleted_behind_the_view_are_not_found():
    faq = FaqFactory()
    faq.soft_delete()
    with pytest.raises(NotFound) as exc:
        faq_services.publish(faq, user=None)
    assert exc.value.code == "faq_not_found"
    page = PageFactory()
    page.soft_delete()
    with pytest.raises(NotFound) as exc:
        pages.publish(page, user=None)
    assert exc.value.code == "page_not_found"


def test_deleted_references_are_refused():
    category = FaqCategoryFactory()
    category.soft_delete()
    page = PageFactory()
    page.soft_delete()
    asset = MediaAssetFactory()
    asset.soft_delete()
    with pytest.raises(DomainError) as exc:
        faq_services.create_faq(user=None, data={"question": "Q?", "category": category, "page": page, "og_image": asset, "schema_extra": "x"})
    assert set(exc.value.errors) == {"category", "page", "og_image", "schema_extra"}
    with pytest.raises(DomainError) as exc:
        content.update_image_slot(PageImageSlotFactory(key="hero").page, "hero", user=None, data={"asset": asset})
    assert exc.value.errors == {"asset": ["The file has been deleted."]}


def test_url_slot_rejects_a_bare_word_and_accepts_site_paths():
    slot = PageTextSlotFactory(kind="URL", max_length=None)
    assert content.text_value_problem(slot, "/career") is None and content.text_value_problem(slot, "career")


@pytest.mark.parametrize("module", ["faqs"])
def test_scope_all_sees_every_record(auth_client, make_user, module):
    FaqFactory.create_batch(3)
    FaqCategoryFactory.create_batch(2)
    client = auth_client(make_user(grants={module: ["view"]}, scopes={module: "all"}))
    assert client.get("/api/v1/faqs/").json()["count"] == 3 and client.get("/api/v1/faq-categories/").json()["count"] == 2


def test_public_payload_cost_does_not_grow_with_rows(api_client, django_assert_max_num_queries):
    page = PageFactory(route="/cost", slug="cost")
    for _ in range(3):
        PublishedFaqFactory(page=page, category=FaqCategoryFactory())
        PageImageSlotFactory(page=page, asset=MediaAssetFactory())
        PageTextSlotFactory(page=page, value="x")
    with django_assert_max_num_queries(10) as faqs_few:
        api_client.get("/api/public/v1/faqs/", {"route": "/cost"})
    with django_assert_max_num_queries(10) as page_few:
        api_client.get("/api/public/v1/pages/cost/")
    for _ in range(10):
        PublishedFaqFactory(page=page, category=FaqCategoryFactory())
        PageImageSlotFactory(page=page, asset=MediaAssetFactory())
        PageTextSlotFactory(page=page, value="x")
    cache.clear()
    with django_assert_max_num_queries(len(faqs_few.captured_queries)):
        assert api_client.get("/api/public/v1/faqs/", {"route": "/cost"}).json()["meta"]["count"] == 13
    with django_assert_max_num_queries(len(page_few.captured_queries)):
        assert len(api_client.get("/api/public/v1/pages/cost/").json()["data"]["images"]) == 13


def test_faq_model_constraints():
    from django.db import IntegrityError, transaction

    from faqs.models import Faq, FaqCategory

    for values in ({"status": "LIVE"}, {"schema_type": "Recipe"}):
        with pytest.raises(IntegrityError), transaction.atomic():
            Faq.objects.create(question="Q?", **values)
    FaqCategoryFactory(name="Same", slug="same")
    for values in ({"name": "Same", "slug": "other"}, {"name": "Other", "slug": "same"}):
        with pytest.raises(IntegrityError), transaction.atomic():
            FaqCategory.objects.create(**values)
