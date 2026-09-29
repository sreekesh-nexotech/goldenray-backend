"""faq-categories/ — CRUD with live-unique name/slug and the in-use delete guard."""

import pytest

from audit.models import AuditLog
from core.models import OutboxEvent
from faqs.models import FaqCategory
from faqs.tests.factories import FaqCategoryFactory, FaqFactory, PublishedFaqFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/faq-categories/"


@pytest.fixture
def client(auth_client, make_user):
    return auth_client(make_user(grants={"faqs": "*"}))


def detail(category):
    return f"{URL}{category.uid}/"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        category = FaqCategoryFactory()
        for method, path in [("get", URL), ("post", URL), ("get", detail(category)), ("patch", detail(category)), ("delete", detail(category))]:
            assert getattr(api_client, method)(path).status_code == 401

    @pytest.mark.parametrize("method,path,needed", [("get", "list", "view"), ("get", "detail", "view"), ("post", "list", "create"), ("patch", "detail", "edit"), ("delete", "detail", "archive")])
    def test_each_action_needs_its_permission(self, auth_client, make_user, method, path, needed):
        category = FaqCategoryFactory()
        client = auth_client(make_user(grants={"faqs": [a for a in ("view", "create", "edit", "publish", "verify", "archive") if a != needed], "pages": "*"}))
        target = URL if path == "list" else detail(category)
        assert getattr(client, method)(target, {"name": "X"}, format="json").status_code == 403


class TestCrud:
    def test_create_derives_the_slug_and_lists_counts(self, client):
        response = client.post(URL, {"name": "  PM Surya Ghar  ", "description": "Subsidy"}, format="json")
        assert response.status_code == 201
        body = response.json()
        assert (body["name"], body["slug"], body["is_active"], body["faq_count"]) == ("PM Surya Ghar", "pm-surya-ghar", True, 0)
        category = FaqCategory.objects.get()
        FaqFactory(category=category)
        PublishedFaqFactory(category=category)
        FaqFactory(category=category).soft_delete()
        row = client.get(URL).json()["results"][0]
        assert (row["faq_count"], row["published_faq_count"]) == (2, 1)
        assert AuditLog.objects.filter(action="faqs.category_created").exists()

    def test_validation_and_uniqueness(self, client):
        FaqCategoryFactory(name="Subsidy", slug="subsidy")
        assert client.post(URL, {}, format="json").json()["errors"]["name"]
        assert client.post(URL, {"name": "   "}, format="json").json()["errors"]["name"]
        assert client.post(URL, {"name": "!!!"}, format="json").json()["errors"]["slug"]
        taken = client.post(URL, {"name": "Subsidy"}, format="json")
        assert taken.status_code == 409 and taken.json()["code"] == "category_name_taken"
        taken = client.post(URL, {"name": "Other", "slug": "subsidy"}, format="json")
        assert taken.status_code == 409 and taken.json()["code"] == "category_slug_taken"

    def test_update_rename_emits_for_pages_showing_it(self, client):
        category = FaqCategoryFactory(name="Money", slug="money")
        faq = PublishedFaqFactory(category=category)
        FaqFactory(category=category)
        response = client.patch(detail(category), {"name": "Financing", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["name"] == "Financing" and response.json()["slug"] == "money" and response.json()["version"] == 2
        event = OutboxEvent.objects.get(event_type="faqs.category_updated")
        assert event.payload["paths"] == [faq.page.route]
        assert client.patch(detail(category), {"is_active": False}, format="json").json()["is_active"] is False
        assert OutboxEvent.objects.filter(event_type="faqs.category_updated").count() == 1  # only renames change the site
        assert client.patch(detail(category), {"is_active": False}, format="json").json()["version"] == 3  # no-op

    def test_update_conflicts_and_stale_version(self, client):
        FaqCategoryFactory(name="Taken", slug="taken")
        category = FaqCategoryFactory(version=2)
        assert client.patch(detail(category), {"slug": "taken"}, format="json").json()["code"] == "category_slug_taken"
        assert client.patch(detail(category), {"slug": ""}, format="json").status_code == 400
        assert client.patch(detail(category), {"name": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"

    def test_delete_is_guarded_while_in_use(self, client):
        category = FaqCategoryFactory()
        faq = FaqFactory(category=category, status="ARCHIVED")
        response = client.delete(detail(category))
        assert response.status_code == 409 and response.json()["code"] == "category_in_use" and response.json()["message"].startswith("1 FAQ(s) use this category.")
        faq.soft_delete()
        assert client.delete(detail(category)).status_code == 204
        assert not FaqCategory.objects.exists() and FaqCategory.all_objects.count() == 1
        assert client.post(URL, {"name": category.name, "slug": category.slug}, format="json").status_code == 201  # name/slug freed

    def test_filters_and_list_cost(self, client, django_assert_max_num_queries):
        FaqCategoryFactory(is_active=False, name="Old")
        FaqCategoryFactory(name="New")
        assert [row["name"] for row in client.get(URL, {"is_active": "false"}).json()["results"]] == ["Old"]
        with django_assert_max_num_queries(6) as few:
            client.get(URL)
        for category in FaqCategoryFactory.create_batch(10):
            FaqFactory(category=category)
        with django_assert_max_num_queries(len(few.captured_queries)):
            client.get(URL)


def test_integrity_race_maps_to_the_right_conflict():
    from django.db import IntegrityError

    from faqs.services.categories import _from_integrity_error

    assert _from_integrity_error(IntegrityError('duplicate key value violates unique constraint "faqs_category_slug_uniq"')).code == "category_slug_taken"
    assert _from_integrity_error(IntegrityError('duplicate key value violates unique constraint "faqs_category_name_uniq"')).code == "category_name_taken"
