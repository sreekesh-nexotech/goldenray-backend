"""pages/ — list, detail, PATCH (sort order) and the lifecycle actions (publish/unpublish/archive/restore/verify)."""

import pytest

from audit.models import AuditLog
from core.models import OutboxEvent
from faqs.tests.factories import FaqFactory
from sitepages.models import Page
from sitepages.tests.factories import PageFactory, PageImageSlotFactory, PageSeoFactory, PageTextSlotFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/pages/"


@pytest.fixture
def manager(make_user):
    return make_user(grants={"pages": "*"})


@pytest.fixture
def client(auth_client, manager):
    return auth_client(manager)


def detail(page, suffix=""):
    return f"{URL}{page.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        page = PageFactory()
        for method, path in [("get", URL), ("get", detail(page)), ("patch", detail(page)), ("post", detail(page, "publish/")), ("get", detail(page, "seo/")), ("patch", detail(page, "seo/"))]:
            assert getattr(api_client, method)(path).status_code == 401, (method, path)

    @pytest.mark.parametrize(
        "method,suffix,needed",
        [
            ("get", None, "view"),
            ("get", "", "view"),
            ("get", "seo/", "view"),
            ("get", "preview/", "view"),
            ("patch", "", "edit"),
            ("patch", "seo/", "edit"),
            ("patch", "text-slots/headline/", "edit"),
            ("patch", "image-slots/hero/", "edit"),
            ("post", "publish/", "publish"),
            ("post", "unpublish/", "publish"),
            ("post", "archive/", "publish"),
            ("post", "restore/", "publish"),
            ("post", "verify/", "verify"),
        ],
    )
    def test_each_action_needs_its_permission(self, auth_client, make_user, method, suffix, needed):
        page = PageFactory()
        PageTextSlotFactory(page=page, key="headline")
        PageImageSlotFactory(page=page, key="hero")
        others = [action for action in ("view", "edit", "publish", "verify") if action != needed]
        client = auth_client(make_user(grants={"pages": others, "career_page": "*", "seo": "*", "faqs": "*"}))
        target = URL if suffix is None else detail(page, suffix)
        assert getattr(client, method)(target, {}, format="json").status_code == 403

    def test_no_create_and_no_delete(self, client):
        """Pages are registered routes: create/delete/PUT are not routed, so the default deny answers first."""
        page = PageFactory()
        assert client.post(URL, {"slug": "x", "route": "/x", "title": "X"}, format="json").status_code == 403
        assert client.delete(detail(page)).status_code == 403
        assert client.put(detail(page), {"sort_order": 1}, format="json").status_code == 403
        assert Page.objects.count() == 1

    def test_scope_all_sees_every_page(self, auth_client, make_user):
        PageFactory.create_batch(3)
        user = make_user(grants={"pages": ["view"]}, scopes={"pages": "all"})
        assert auth_client(user).get(URL).json()["count"] == 3


class TestList:
    def test_shape_counts_and_seo_status(self, client):
        page = PageFactory(title="Careers", group="Careers")
        PageTextSlotFactory.create_batch(2, page=page)
        PageImageSlotFactory(page=page)
        PageSeoFactory(page=page)
        FaqFactory(page=page)
        FaqFactory(page=page, status="ARCHIVED")
        FaqFactory(page=page).soft_delete()
        PageFactory(title="Bare")
        body = client.get(URL).json()
        assert body["count"] == 2
        row = next(item for item in body["results"] if item["title"] == "Careers")
        assert set(row) == {
            "uid",
            "slug",
            "route",
            "title",
            "group",
            "template",
            "status",
            "is_protected",
            "sort_order",
            "seo_status",
            "image_slot_count",
            "text_slot_count",
            "faq_count",
            "verified_at",
            "updated_at",
            "version",
        }
        assert (row["text_slot_count"], row["image_slot_count"], row["faq_count"]) == (2, 1, 1)
        assert row["seo_status"] == "ok"
        assert next(item for item in body["results"] if item["title"] == "Bare")["seo_status"] == "error"

    def test_filters_search_ordering(self, client):
        PageFactory(title="Alpha", group="Tools", status="DRAFT", sort_order=2)
        PageFactory(title="Beta", group="Legal", sort_order=1, route="/legal/beta-terms")
        PageFactory(title="Gone").soft_delete()
        assert [row["title"] for row in client.get(URL).json()["results"]] == ["Beta", "Alpha"]
        assert [row["title"] for row in client.get(URL, {"status": "DRAFT"}).json()["results"]] == ["Alpha"]
        assert [row["title"] for row in client.get(URL, {"filter[group]": "Legal"}).json()["results"]] == ["Beta"]
        assert [row["title"] for row in client.get(URL, {"search": "beta-terms"}).json()["results"]] == ["Beta"]
        assert [row["title"] for row in client.get(URL, {"ordering": "-title"}).json()["results"]] == ["Beta", "Alpha"]
        assert client.get(URL, {"status": "NOPE"}).status_code == 400

    def test_verified_filter(self, client, manager):
        verified = PageFactory(title="Checked")
        PageFactory(title="Unchecked")
        client.post(detail(verified, "verify/"))
        assert [row["title"] for row in client.get(URL, {"verified": "true"}).json()["results"]] == ["Checked"]
        assert [row["title"] for row in client.get(URL, {"verified": "false"}).json()["results"]] == ["Unchecked"]

    def test_listing_cost_does_not_grow_with_rows(self, client, django_assert_max_num_queries):
        for _ in range(3):
            page = PageFactory()
            PageSeoFactory(page=page)
            PageTextSlotFactory(page=page)
            FaqFactory(page=page)
        with django_assert_max_num_queries(8) as few:
            client.get(URL)
        for _ in range(10):
            page = PageFactory()
            PageSeoFactory(page=page)
            PageImageSlotFactory(page=page)
            FaqFactory(page=page)
        with django_assert_max_num_queries(len(few.captured_queries)):
            assert client.get(URL).json()["count"] == 13


class TestDetail:
    def test_detail_carries_slots_and_seo(self, client):
        page = PageFactory(description="What this page is for")
        PageTextSlotFactory(page=page, key="headline", value="Hello")
        PageImageSlotFactory(page=page, key="hero")
        PageTextSlotFactory(page=page, key="gone").soft_delete()
        body = client.get(detail(page)).json()
        assert body["description"] == "What this page is for" and body["seo"] is None and body["verified_by"] is None
        assert [slot["key"] for slot in body["text_slots"]] == ["headline"] and body["text_slots"][0]["value"] == "Hello"
        assert [slot["key"] for slot in body["image_slots"]] == ["hero"] and body["image_slots"][0]["asset"] is None
        PageSeoFactory(page=page)
        assert client.get(detail(page)).json()["seo"]["seo_title"] == "A page about solar"

    def test_unknown_and_deleted_pages_are_404(self, client):
        page = PageFactory()
        page.soft_delete()
        assert client.get(detail(page)).status_code == 404
        assert client.get(f"{URL}00000000-0000-0000-0000-000000000000/").status_code == 404


class TestUpdate:
    def test_sort_order_only(self, client):
        page = PageFactory(sort_order=3, title="Keep")
        response = client.patch(detail(page), {"sort_order": 7, "title": "Renamed", "status": "ARCHIVED", "expected_version": 1}, format="json")
        assert response.status_code == 200
        body = response.json()
        assert (body["sort_order"], body["title"], body["status"], body["version"]) == (7, "Keep", "PUBLISHED", 2)
        entry = AuditLog.objects.get(action="sitepages.page_updated")
        assert entry.before == {"sort_order": 3} and entry.after == {"sort_order": 7}

    def test_no_change_writes_nothing(self, client):
        page = PageFactory(sort_order=3)
        assert client.patch(detail(page), {"sort_order": 3}, format="json").json()["version"] == 1
        assert not AuditLog.objects.filter(action="sitepages.page_updated").exists()

    def test_validation_and_stale_version(self, client):
        page = PageFactory(version=4)
        invalid = client.patch(detail(page), {"sort_order": "first"}, format="json")
        assert invalid.status_code == 400 and invalid.json()["code"] == "validation_error" and "sort_order" in invalid.json()["errors"]
        stale = client.patch(detail(page), {"sort_order": 1, "expected_version": 3}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"


class TestWorkflow:
    @pytest.mark.parametrize(
        "start,action,end,event",
        [
            ("DRAFT", "publish", "PUBLISHED", "sitepages.page_published"),
            ("ARCHIVED", "publish", "PUBLISHED", "sitepages.page_published"),
            ("PUBLISHED", "unpublish", "DRAFT", "sitepages.page_unpublished"),
            ("PUBLISHED", "archive", "ARCHIVED", "sitepages.page_archived"),
            ("DRAFT", "archive", "ARCHIVED", None),
            ("ARCHIVED", "restore", "DRAFT", None),
        ],
    )
    def test_transitions(self, client, start, action, end, event):
        page = PageFactory(status=start)
        response = client.post(detail(page, f"{action}/"), {"expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["status"] == end and response.json()["version"] == 2
        entry = AuditLog.objects.get(action__startswith="sitepages.page_", object_uid=page.uid)
        assert entry.before == {"status": start} and entry.after == {"status": end}
        events = list(OutboxEvent.objects.filter(aggregate_uid=page.uid))
        if event is None:
            assert events == []
        else:
            assert [e.event_type for e in events] == [event]
            assert events[0].payload["paths"] == [page.route] and events[0].payload["previous_status"] == start

    @pytest.mark.parametrize("start,action", [("ARCHIVED", "unpublish"), ("PUBLISHED", "restore")])
    def test_invalid_transitions(self, client, start, action):
        page = PageFactory(status=start)
        response = client.post(detail(page, f"{action}/"))
        assert response.status_code == 409 and response.json()["code"] == "invalid_transition"

    @pytest.mark.parametrize("status,action", [("PUBLISHED", "publish"), ("DRAFT", "unpublish"), ("ARCHIVED", "archive"), ("DRAFT", "restore")])
    def test_same_state_is_a_no_op(self, client, status, action):
        page = PageFactory(status=status)
        response = client.post(detail(page, f"{action}/"))
        assert response.status_code == 200 and response.json()["version"] == 1
        assert not OutboxEvent.objects.exists()

    def test_stale_version(self, client):
        page = PageFactory(status="DRAFT", version=2)
        response = client.post(detail(page, "publish/"), {"expected_version": 1}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        page.refresh_from_db()
        assert page.status == "DRAFT"

    def test_verify_and_content_change_clears_it(self, client, manager):
        page = PageFactory()
        PageTextSlotFactory(page=page, key="headline")
        body = client.post(detail(page, "verify/"), {"expected_version": 1}, format="json").json()
        assert body["verified_at"] and body["verified_by"]["uid"] == str(manager.uid)
        assert AuditLog.objects.filter(action="sitepages.page_verified", object_uid=page.uid).exists()
        client.patch(detail(page, "text-slots/headline/"), {"value": "New"}, format="json")
        page.refresh_from_db()
        assert page.verified_at is None and page.verified_by is None and page.version == 3

    def test_verify_refuses_archived_and_stale(self, client):
        archived = PageFactory(status="ARCHIVED")
        response = client.post(detail(archived, "verify/"))
        assert response.status_code == 409 and response.json()["code"] == "invalid_transition"
        page = PageFactory(version=5)
        assert client.post(detail(page, "verify/"), {"expected_version": 4}, format="json").status_code == 409

    def test_action_body_validation(self, client):
        page = PageFactory()
        response = client.post(detail(page, "publish/"), {"expected_version": "zero"}, format="json")
        assert response.status_code == 400 and "expected_version" in response.json()["errors"]


def test_dashboard_counts(auth_client, make_user):
    PageFactory.create_batch(2)
    PageFactory(status="DRAFT")
    body = auth_client(make_user(grants={"pages": ["view"], "dashboard": ["view"]})).get("/api/v1/dashboard/").json()
    assert body["modules"]["pages"] == {"published": 2, "draft": 1, "unverified": 2}


def test_page_model_constraints():
    from django.db import IntegrityError, transaction

    PageFactory(slug="dup", route="/dup")
    for values in (
        {"slug": "dup", "route": "/other"},
        {"slug": "other", "route": "/dup"},
        {"slug": "Bad Slug", "route": "/x"},
        {"slug": "ok", "route": "no-slash"},
        {"slug": "ok2", "route": "/y", "status": "LIVE"},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            Page.objects.create(title="x", **values)
    Page.objects.get(slug="dup").soft_delete()
    assert PageFactory(slug="dup", route="/dup").pk  # soft-deleted rows free their slug and route
