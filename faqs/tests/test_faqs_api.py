"""faqs/ — list (filters, archived handling, cost), detail, create, edit, preview."""

import pytest

from audit.models import AuditLog
from core.models import OutboxEvent
from faqs.models import Faq
from faqs.tests.factories import FaqCategoryFactory, FaqFactory, PublishedFaqFactory
from media.tests.factories import MediaAssetFactory
from sitepages.tests.factories import PageFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/faqs/"


@pytest.fixture
def editor(make_user):
    return make_user(grants={"faqs": "*"})


@pytest.fixture
def client(auth_client, editor):
    return auth_client(editor)


def detail(faq, suffix=""):
    return f"{URL}{faq.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        faq = FaqFactory()
        for method, path in [("get", URL), ("post", URL), ("get", detail(faq)), ("patch", detail(faq)), ("post", detail(faq, "publish/")), ("post", f"{URL}reorder/")]:
            assert getattr(api_client, method)(path).status_code == 401, (method, path)

    @pytest.mark.parametrize(
        "method,suffix,needed",
        [
            ("get", None, "view"),
            ("get", "", "view"),
            ("get", "preview/", "view"),
            ("post", None, "create"),
            ("patch", "", "edit"),
            ("post", "reorder", "edit"),
            ("post", "publish/", "publish"),
            ("post", "unpublish/", "publish"),
            ("post", "archive/", "archive"),
            ("post", "restore/", "archive"),
            ("post", "verify/", "verify"),
        ],
    )
    def test_each_action_needs_its_permission(self, auth_client, make_user, method, suffix, needed):
        faq = FaqFactory()
        client = auth_client(make_user(grants={"faqs": [a for a in ("view", "create", "edit", "publish", "verify", "archive") if a != needed], "pages": "*"}))
        target = {None: URL, "reorder": f"{URL}reorder/"}.get(suffix) or detail(faq, suffix)
        assert getattr(client, method)(target, {}, format="json").status_code == 403

    def test_no_delete(self, client):
        faq = FaqFactory()
        assert client.delete(detail(faq)).status_code == 403
        assert Faq.objects.filter(pk=faq.pk).exists()


class TestList:
    def test_shape_and_archived_hidden_by_default(self, client, editor):
        page = PageFactory(route="/subsidy-test")
        PublishedFaqFactory(page=page, question="Live?", category=FaqCategoryFactory(name="Subsidy"), updated_by=editor)
        FaqFactory(page=page, question="Draft?")
        FaqFactory(page=page, question="Old?", status="ARCHIVED")
        FaqFactory(question="Deleted?").soft_delete()
        body = client.get(URL).json()
        assert [row["question"] for row in body["results"]] == ["Live?", "Draft?"]
        row = body["results"][0]
        assert row["page"] == {"uid": str(page.uid), "slug": page.slug, "route": "/subsidy-test", "title": page.title, "status": "PUBLISHED"}
        assert row["category"]["name"] == "Subsidy" and row["updated_by"]["uid"] == str(editor.uid) and row["seo_status"] in {"ok", "warning", "error"}
        assert "answer" not in row
        assert [r["question"] for r in client.get(URL, {"include_archived": "true"}).json()["results"]] == ["Live?", "Draft?", "Old?"]
        assert [r["question"] for r in client.get(URL, {"status": "ARCHIVED"}).json()["results"]] == ["Old?"]
        assert [r["question"] for r in client.get(URL, {"filter[status]": "ARCHIVED"}).json()["results"]] == ["Old?"]

    def test_filters(self, client):
        page = PageFactory(route="/emi-test")
        category = FaqCategoryFactory()
        FaqFactory(page=page, section="", question="Blank section")
        FaqFactory(page=page, section="banks", question="Banks", category=category)
        FaqFactory(question="Elsewhere", answer="rooftop answer")
        ask = lambda **params: [row["question"] for row in client.get(URL, params).json()["results"]]  # noqa: E731
        assert ask(route="/emi-test") == ["Blank section", "Banks"]
        assert ask(page_uid=str(page.uid), section="banks") == ["Banks"]
        assert ask(route="/emi-test", section="") == ["Blank section"]  # present-but-empty = the unnamed section
        assert ask(category=str(category.uid)) == ["Banks"]
        assert ask(search="rooftop") == ["Elsewhere"]
        assert ask(route="/emi-test", ordering="-question") == ["Blank section", "Banks"]
        assert client.get(URL, {"page_uid": "not-a-uuid"}).status_code == 400

    def test_listing_cost_does_not_grow_with_rows(self, client, django_assert_max_num_queries):
        for _ in range(3):
            PublishedFaqFactory(category=FaqCategoryFactory(), og_image=MediaAssetFactory())
        with django_assert_max_num_queries(8) as few:
            client.get(URL)
        for _ in range(12):
            PublishedFaqFactory(category=FaqCategoryFactory(), og_image=MediaAssetFactory())
        with django_assert_max_num_queries(len(few.captured_queries)):
            assert client.get(URL).json()["count"] == 15


class TestCreateAndEdit:
    def test_create_lands_at_the_end_of_its_section_as_draft(self, client, editor):
        page = PageFactory()
        FaqFactory(page=page, section="banks", sort_order=4)
        category = FaqCategoryFactory()
        response = client.post(URL, {"question": "  Is there EMI?  ", "answer": "", "page": str(page.uid), "section": "banks", "category": str(category.uid)}, format="json")
        assert response.status_code == 201
        body = response.json()
        assert (body["question"], body["status"], body["sort_order"], body["version"]) == ("Is there EMI?", "DRAFT", 5, 1)
        assert body["publish_errors"] == ["An answer is required."] and body["created_by"]["uid"] == str(editor.uid)
        assert AuditLog.objects.filter(action="faqs.faq_created").exists() and not OutboxEvent.objects.exists()
        first = client.post(URL, {"question": "First in a new section?", "page": str(page.uid), "section": "new"}, format="json").json()
        assert first["sort_order"] == 0

    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"question": ""}, "question"),
            ({"question": "   "}, "question"),
            ({"question": "Q", "page": "00000000-0000-0000-0000-000000000000"}, "page"),
            ({"question": "Q", "category": "00000000-0000-0000-0000-000000000000"}, "category"),
            ({"question": "Q", "schema_type": "Recipe"}, "schema_type"),
            ({"question": "Q", "schema_extra": {"x": "y" * 20000}}, "schema_extra"),
            ({"question": "Q", "sort_order": -1}, "sort_order"),
            ({"question": "Q" * 501}, "question"),
        ],
    )
    def test_create_validation(self, client, payload, field):
        response = client.post(URL, payload, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_og_image_must_be_public(self, client):
        private = MediaAssetFactory(visibility="PRIVATE", cdn_url="")
        response = client.post(URL, {"question": "Q", "og_image": str(private.uid)}, format="json")
        assert response.status_code == 400 and response.json()["errors"]["og_image"]

    def test_edit_published_emits_for_old_and_new_page_and_clears_review(self, client, editor):
        old_page, new_page = PageFactory(), PageFactory()
        faq = PublishedFaqFactory(page=old_page, verified_by=editor)
        Faq.objects.filter(pk=faq.pk).update(verified_at=faq.created_at)
        FaqFactory(page=new_page, sort_order=2)
        response = client.patch(detail(faq), {"page": str(new_page.uid), "answer": "Moved.", "expected_version": 1}, format="json")
        assert response.status_code == 200
        body = response.json()
        assert body["page"]["uid"] == str(new_page.uid) and body["sort_order"] == 3 and body["verified_at"] is None and body["version"] == 2
        event = OutboxEvent.objects.get(event_type="faqs.faq_updated")
        assert event.payload["paths"] == sorted([old_page.route, new_page.route])
        entry = AuditLog.objects.get(action="faqs.faq_updated")
        assert entry.after["answer"] == "Moved."

    def test_published_faq_must_stay_complete(self, client):
        faq = PublishedFaqFactory()
        response = client.patch(detail(faq), {"answer": "  "}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "faq_not_publishable" and response.json()["errors"]["publish"] == ["An answer is required."]
        assert client.patch(detail(faq), {"page": None}, format="json").json()["errors"]["publish"] == ["Choose the page this FAQ appears on."]

    def test_draft_seo_edit_emits_nothing_and_no_change_is_a_no_op(self, client):
        faq = FaqFactory()
        assert client.patch(detail(faq), {"seo_title": "FAQ", "noindex": True}, format="json").json()["version"] == 2
        assert client.patch(detail(faq), {"seo_title": "FAQ"}, format="json").json()["version"] == 2
        published = PublishedFaqFactory()
        client.patch(detail(published), {"meta_description": "Only SEO"}, format="json")
        assert not OutboxEvent.objects.exists()

    def test_stale_version(self, client):
        faq = FaqFactory(version=3)
        response = client.patch(detail(faq), {"question": "New?", "expected_version": 2}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_detail_reaches_archived_and_shows_publish_errors(self, client):
        faq = FaqFactory(status="ARCHIVED", answer="")
        body = client.get(detail(faq)).json()
        assert body["status"] == "ARCHIVED" and body["publish_errors"] == ["An answer is required."] and body["answer"] == ""


class TestPreview:
    def test_preview_places_a_draft_among_its_published_siblings(self, client, settings):
        settings.FRONTEND_BASE_URL = "https://flarize.com"
        page = PageFactory(route="/emi-preview")
        PublishedFaqFactory(page=page, question="First?", sort_order=0)
        PublishedFaqFactory(page=page, question="Third?", sort_order=2)
        draft = FaqFactory(page=page, question="Second?", answer="Yes.", sort_order=1)
        body = client.get(detail(draft, "preview/")).json()
        assert body["url"] == "https://flarize.com/emi-preview" and body["position"] == 1 and body["status"] == "DRAFT"
        assert [entity["name"] for entity in body["schema"]["mainEntity"]] == ["First?", "Second?", "Third?"]
        orphan = FaqFactory(page=None)
        body = client.get(detail(orphan, "preview/")).json()
        assert body["page"] is None and body["schema"] is None and "Choose the page this FAQ appears on." in body["publish_errors"]


def test_dashboard_counts(auth_client, make_user):
    PublishedFaqFactory.create_batch(2)
    FaqFactory(status="ARCHIVED")
    body = auth_client(make_user(grants={"faqs": ["view"], "dashboard": ["view"]})).get("/api/v1/dashboard/").json()
    assert body["modules"]["faqs"] == {"published": 2, "draft": 0, "archived": 1}
