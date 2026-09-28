"""Website revalidation: events → outbox handler → thin client (fake backend in tests; fail-soft; signed; deduplicated)."""

import hashlib
import hmac
import json
import logging

import pytest
import requests

from blog.services import revalidation, workflow
from blog.tests.factories import EntryFactory, PublishedEntryFactory
from company.tests.factories import CompanyProfileFactory
from core.models import OutboxEvent
from core.outbox import emit

pytestmark = pytest.mark.django_db
TARGET = "https://flarize.com/api/revalidate"


@pytest.fixture
def configured():
    return CompanyProfileFactory(blog_revalidate_url=TARGET, blog_revalidate_secret="shared-secret")


def test_publish_posts_each_path_with_the_secret_and_a_signature(configured, fake_revalidation, drain_outbox, make_user):
    entry = EntryFactory(slug="net-metering")
    workflow.publish_entry(entry, user=make_user(grants={"blogs": "*"}))
    drain_outbox()
    prefix = entry.collection.path_prefix
    assert [sent["body"] for sent in fake_revalidation.sent] == [{"secret": "shared-secret", "path": prefix}, {"secret": "shared-secret", "path": f"{prefix}/net-metering"}]
    sent = fake_revalidation.sent[0]
    assert sent["url"] == TARGET
    raw = json.dumps(sent["body"], separators=(",", ":")).encode()
    expected = hmac.new(b"shared-secret", f"{sent['headers']['X-Flarize-Timestamp']}.".encode() + raw, hashlib.sha256).hexdigest()
    assert sent["headers"]["X-Flarize-Signature"] == f"sha256={expected}"


@pytest.mark.parametrize("action", ["unpublish", "archive"])
def test_taking_an_entry_down_revalidates(configured, fake_revalidation, drain_outbox, action):
    entry = PublishedEntryFactory()
    getattr(workflow, f"{action}_entry")(entry, user=None)
    drain_outbox()
    assert {sent["body"]["path"] for sent in fake_revalidation.sent} == {entry.collection.path_prefix, f"{entry.collection.path_prefix}/{entry.slug}"}


def test_events_are_deduplicated_per_entry_version(configured):
    entry = PublishedEntryFactory()
    from blog.services.entries import publication_event

    publication_event(entry, "blog.entry_updated")
    publication_event(entry, "blog.entry_updated")
    assert OutboxEvent.objects.filter(event_type="blog.entry_updated").count() == 1


def test_no_url_or_no_secret_sends_nothing(fake_revalidation, drain_outbox):
    CompanyProfileFactory(blog_revalidate_url=TARGET, blog_revalidate_secret=None)
    assert revalidation.revalidate(["/blog"]) == []
    emit("website.revalidate_requested", {"paths": ["/blog"]})
    drain_outbox()
    assert fake_revalidation.sent == []


def test_off_backend_and_invalid_paths(configured, fake_revalidation, settings):
    assert revalidation.revalidate(["blog", "//evil.example", "/ok", "/ok"]) == [revalidation.Result(path="/ok", status=200)]
    settings.BLOG_REVALIDATE_BACKEND = "off"
    assert revalidation.revalidate(["/blog"]) == []


def test_rejections_and_errors_are_logged_never_raised(configured, fake_revalidation, drain_outbox, caplog):
    fake_revalidation.status = 401
    with caplog.at_level(logging.WARNING, logger="flarize.blog.revalidation"):
        results = revalidation.revalidate(["/blog"])
    assert results[0].status == 401 and not results[0].ok and "rejected" in caplog.text
    fake_revalidation.fail_with = requests.ConnectionError
    assert revalidation.revalidate(["/blog"]) == [revalidation.Result(path="/blog", status=None, error="ConnectionError")]
    emit("website.revalidate_requested", {"paths": ["/faq"]})
    assert drain_outbox()["processed"] == 1  # fail-soft: the event is done, never parked


def test_undecryptable_secret_disables_pings(configured, fake_revalidation, caplog):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("UPDATE company_profile SET blog_revalidate_secret = 'not-a-fernet-token'")
    with caplog.at_level(logging.ERROR, logger="flarize.blog.revalidation"):
        assert revalidation.revalidate(["/blog"]) == []
    assert "cannot be decrypted" in caplog.text and fake_revalidation.sent == []


def test_http_backend_posts_with_a_timeout(monkeypatch, settings):
    calls = {}

    class Reply:
        status_code = 204

    def fake_post(url, **kwargs):
        calls.update(url=url, **kwargs)
        return Reply()

    monkeypatch.setattr(revalidation.requests, "post", fake_post)
    settings.BLOG_REVALIDATE_TIMEOUT_SECONDS = 2
    assert revalidation.HttpBackend().post(TARGET, b"{}", {"X": "1"}) == 204
    assert calls["timeout"] == 2.0 and calls["allow_redirects"] is False and calls["data"] == b"{}"
    settings.BLOG_REVALIDATE_BACKEND = "http"
    assert isinstance(revalidation.backend(), revalidation.HttpBackend)


def test_generic_website_event_is_handled(configured, fake_revalidation, drain_outbox):
    emit("website.revalidate_requested", {"paths": ["/faq", "/career"]})
    drain_outbox()
    assert [sent["body"]["path"] for sent in fake_revalidation.sent] == ["/faq", "/career"]
