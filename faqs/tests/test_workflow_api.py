"""faqs/<uid>/publish|unpublish|archive|restore|verify/ and faqs/reorder/."""

import uuid

import pytest

from audit.models import AuditLog
from core.models import OutboxEvent
from faqs.models import Faq
from faqs.tests.factories import FaqFactory, PublishedFaqFactory
from sitepages.tests.factories import PageFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/faqs/"


@pytest.fixture
def editor(make_user):
    return make_user(grants={"faqs": "*"})


@pytest.fixture
def client(auth_client, editor):
    return auth_client(editor)


def act(client, faq, action, **body):
    return client.post(f"{URL}{faq.uid}/{action}/", body, format="json")


class TestTransitions:
    @pytest.mark.parametrize(
        "start,action,end,event",
        [
            ("DRAFT", "publish", "PUBLISHED", "faqs.faq_published"),
            ("ARCHIVED", "publish", "PUBLISHED", "faqs.faq_published"),
            ("PUBLISHED", "unpublish", "DRAFT", "faqs.faq_unpublished"),
            ("PUBLISHED", "archive", "ARCHIVED", "faqs.faq_archived"),
            ("DRAFT", "archive", "ARCHIVED", None),
            ("ARCHIVED", "restore", "DRAFT", None),
        ],
    )
    def test_transitions(self, client, start, action, end, event):
        faq = FaqFactory(status=start)
        response = act(client, faq, action, expected_version=1)
        assert response.status_code == 200 and response.json()["status"] == end and response.json()["version"] == 2
        entry = AuditLog.objects.get(action__startswith="faqs.faq_", object_uid=faq.uid)
        assert entry.before == {"status": start} and entry.after == {"status": end}
        events = list(OutboxEvent.objects.filter(aggregate_uid=faq.uid))
        assert [e.event_type for e in events] == ([event] if event else [])
        if event:
            assert events[0].payload["paths"] == [faq.page.route] and events[0].payload["previous_status"] == start

    def test_published_at_is_sticky_and_archived_at_follows_the_archive(self, client):
        faq = FaqFactory()
        first = act(client, faq, "publish").json()["published_at"]
        assert first
        act(client, faq, "archive")
        faq.refresh_from_db()
        assert faq.archived_at is not None
        republished = act(client, faq, "publish").json()
        assert republished["published_at"] == first and republished["archived_at"] is None
        act(client, faq, "archive")
        assert act(client, faq, "restore").json()["archived_at"] is None

    def test_publish_refuses_incomplete_faqs_with_the_reasons(self, client):
        archived_page = PageFactory(status="ARCHIVED", title="Old page")
        faq = FaqFactory(answer="", page=archived_page)
        response = act(client, faq, "publish")
        assert response.status_code == 400 and response.json()["code"] == "faq_not_publishable"
        assert response.json()["errors"]["publish"] == ["An answer is required.", "'Old page' is archived — restore it or pick another page."]
        assert act(client, FaqFactory(page=None), "publish").json()["errors"]["publish"] == ["Choose the page this FAQ appears on."]
        blank_question = FaqFactory()
        Faq.objects.filter(pk=blank_question.pk).update(question=" ")
        assert "A question is required." in act(client, blank_question, "publish").json()["errors"]["publish"]

    @pytest.mark.parametrize("start,action", [("ARCHIVED", "unpublish"), ("PUBLISHED", "restore")])
    def test_invalid_transitions(self, client, start, action):
        response = act(client, FaqFactory(status=start), action)
        assert response.status_code == 409 and response.json()["code"] == "invalid_transition"

    @pytest.mark.parametrize("status,action", [("PUBLISHED", "publish"), ("DRAFT", "unpublish"), ("ARCHIVED", "archive"), ("DRAFT", "restore")])
    def test_same_state_is_a_no_op(self, client, status, action):
        faq = FaqFactory(status=status)
        assert act(client, faq, action).json()["version"] == 1 and not OutboxEvent.objects.exists()

    def test_stale_version(self, client):
        faq = FaqFactory(version=4)
        response = act(client, faq, "publish", expected_version=3)
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_verify(self, client, editor):
        faq = PublishedFaqFactory()
        body = act(client, faq, "verify", expected_version=1).json()
        assert body["verified_at"] and body["verified_by"]["uid"] == str(editor.uid) and body["version"] == 2
        assert AuditLog.objects.filter(action="faqs.faq_verified").exists()
        response = act(client, FaqFactory(status="ARCHIVED"), "verify")
        assert response.status_code == 409 and response.json()["code"] == "invalid_transition"
        assert act(client, faq, "verify", expected_version=1).status_code == 409


class TestReorder:
    def reorder(self, client, page, order, section=""):
        return client.post(f"{URL}reorder/", {"page": str(page.uid), "section": section, "order": [str(o) for o in order]}, format="json")

    def test_listed_first_rest_keep_their_order_strangers_ignored(self, client):
        page = PageFactory()
        a, b, c, d = (PublishedFaqFactory(page=page, sort_order=i) for i in range(4))
        stranger = PublishedFaqFactory()  # another page
        other_section = PublishedFaqFactory(page=page, section="other", sort_order=0)
        response = self.reorder(client, page, [d.uid, uuid.uuid4(), stranger.uid, b.uid])
        assert response.status_code == 200
        assert [row["uid"] for row in response.json()] == [str(d.uid), str(b.uid), str(a.uid), str(c.uid)]
        assert [row["sort_order"] for row in response.json()] == [0, 1, 2, 3]
        stranger.refresh_from_db()
        other_section.refresh_from_db()
        assert (stranger.page != page, other_section.sort_order) == (True, 0)
        event = OutboxEvent.objects.get(event_type="faqs.faqs_reordered")
        assert event.payload == {"page_uid": str(page.uid), "section": "", "paths": [page.route]}
        entry = AuditLog.objects.get(action="faqs.faqs_reordered")
        assert entry.after["order"] == [str(d.uid), str(b.uid), str(a.uid), str(c.uid)]

    def test_same_order_changes_nothing_and_drafts_do_not_emit(self, client):
        page = PageFactory()
        first, second = FaqFactory(page=page, sort_order=0), FaqFactory(page=page, sort_order=1)
        self.reorder(client, page, [first.uid, second.uid])
        assert not AuditLog.objects.filter(action="faqs.faqs_reordered").exists()
        self.reorder(client, page, [second.uid, first.uid])
        assert AuditLog.objects.filter(action="faqs.faqs_reordered").exists() and not OutboxEvent.objects.exists()

    def test_validation(self, client):
        page = PageFactory()
        faq = FaqFactory(page=page)
        assert self.reorder(client, page, []).status_code == 400
        assert self.reorder(client, page, [faq.uid, faq.uid]).json()["errors"]["order"]
        assert client.post(f"{URL}reorder/", {"page": str(uuid.uuid4()), "order": [str(faq.uid)]}, format="json").json()["errors"]["page"]
        assert client.post(f"{URL}reorder/", {"order": [str(faq.uid)]}, format="json").json()["errors"]["page"]


def test_reorder_is_documented_as_a_plain_list(tmp_path):
    """OpenAPI: ``faqs/reorder/`` answers a plain array of list rows — no pagination envelope, no list filters."""
    import yaml
    from django.core.management import call_command

    target = tmp_path / "schema.yaml"
    call_command("spectacular", "--api-version", "v1", "--file", str(target))
    operation = yaml.safe_load(target.read_text())["paths"]["/api/v1/faqs/reorder/"]["post"]
    assert [parameter["name"] for parameter in operation.get("parameters", [])] == []
    body = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert body["type"] == "array" and body["items"]["$ref"] == "#/components/schemas/FaqList"
