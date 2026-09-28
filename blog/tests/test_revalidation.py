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


@pytest.mark.parametrize("action", ["unpublish", "archive", "delete", "publish", "edit", "author_renamed"])
def test_changes_also_revalidate_the_entrys_old_urls(configured, fake_revalidation, drain_outbox, action):
    """The website renders the article itself under an active alias URL (the frontend reads data[0] and ignores
    meta.redirect), so whatever changes the entry's page — taking it down, putting it up, an edit, a shared record it
    shows — revalidates its alias URLs with its own paths (a retired alias is not)."""
    from blog.services import entries, taxonomy
    from blog.tests.factories import AliasFactory, AuthorFactory

    entry = PublishedEntryFactory(author=AuthorFactory()) if action != "publish" else EntryFactory()
    AliasFactory(entry=entry, slug="old-name")
    AliasFactory(entry=entry, slug="retired-name", active=False)
    if action == "delete":
        entries.delete_entry(entry, user=None)
    elif action == "edit":
        entries.update_entry(entry, user=None, data={"title": "Edited"})
    elif action == "author_renamed":
        taxonomy.update(taxonomy.AUTHOR, entry.author, user=None, data={"name": "Renamed"})
    else:
        getattr(workflow, f"{action}_entry")(entry, user=None)
    drain_outbox()
    prefix = entry.collection.path_prefix
    assert [sent["body"]["path"] for sent in fake_revalidation.sent] == [prefix, f"{prefix}/{entry.slug}", f"{prefix}/old-name"]


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


class _CountingBackend:
    """Answers like a website that is down (``fail``) or slow (each request advances the clock by ``seconds``)."""

    def __init__(self, clock, *, fail=None, seconds=0.0):
        self.clock, self.fail, self.seconds, self.calls = clock, fail, seconds, []

    def post(self, url, body, headers):
        self.calls.append(json.loads(body)["path"])
        self.clock["now"] += self.seconds
        if self.fail is not None:
            raise self.fail("down")
        return 200


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1000.0}
    monkeypatch.setattr(revalidation.time, "monotonic", lambda: state["now"])
    return state


def test_a_website_that_is_down_costs_one_request_per_event(configured, monkeypatch, clock, caplog):
    # Outbox handlers run inside a Celery task with a 120 s hard limit: 100+ paths each waiting for a 3 s timeout
    # would get the drainer killed and the event parked, so the first transport failure ends the batch.
    down = _CountingBackend(clock, fail=requests.ConnectTimeout, seconds=3)
    monkeypatch.setattr(revalidation, "backend", lambda: down)
    paths = [f"/blog/entry-{index}" for index in range(100)]
    with caplog.at_level(logging.WARNING, logger="flarize.blog.revalidation"):
        results = revalidation.revalidate(paths, reason="blog.content_changed")
    assert down.calls == ["/blog/entry-0"] and results == [revalidation.Result(path="/blog/entry-0", status=None, error="ConnectTimeout")]
    assert "99 paths skipped" in caplog.text


def test_a_slow_website_is_bounded_by_the_time_budget(configured, monkeypatch, clock, settings, caplog):
    settings.BLOG_REVALIDATE_BUDGET_SECONDS = 20
    slow = _CountingBackend(clock, seconds=7)
    monkeypatch.setattr(revalidation, "backend", lambda: slow)
    with caplog.at_level(logging.WARNING, logger="flarize.blog.revalidation"):
        results = revalidation.revalidate([f"/p{index}" for index in range(10)])
    assert slow.calls == ["/p0", "/p1", "/p2"] and all(result.ok for result in results) and len(results) == 3
    assert "7 paths skipped" in caplog.text


def test_the_default_budget_fits_the_celery_soft_time_limit(settings):
    assert revalidation.budget_seconds() + float(getattr(settings, "BLOG_REVALIDATE_TIMEOUT_SECONDS", 3)) < settings.CELERY_TASK_SOFT_TIME_LIMIT / 2


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
