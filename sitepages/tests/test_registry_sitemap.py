"""The maintained-pages registry (``seed_pages``) and the sitemap contribution."""

import datetime as dt
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from audit.models import AuditLog
from faqs.tests.factories import FaqFactory, PublishedFaqFactory
from sitepages.models import Page, PageImageSlot, PageTextSlot
from sitepages.services import registry
from sitepages.services.sitemap import sitemap_entries
from sitepages.tests.factories import PageFactory, PageSeoFactory

pytestmark = pytest.mark.django_db


class TestRegistry:
    def test_seed_creates_every_route_and_slot_once(self):
        out = StringIO()
        call_command("seed_pages", stdout=out)
        assert "27 page(s) and 3 slot(s) created." in out.getvalue()
        assert Page.objects.count() == 27 and Page.objects.filter(status="PUBLISHED").count() == 27
        assert Page.objects.get(route="/").slug == "home" and Page.objects.get(route="/career").slug == "career"
        assert set(PageTextSlot.objects.values_list("key", flat=True)) == {"hero_title", "hero_subtitle"}
        assert PageTextSlot.objects.get(key="hero_title").max_length == 90 and PageImageSlot.objects.get().key == "hero_background"
        assert AuditLog.objects.filter(action="sitepages.registry_synced").count() == 1
        out = StringIO()
        call_command("seed_pages", stdout=out)
        assert "0 page(s) and 0 slot(s) created." in out.getvalue() and AuditLog.objects.filter(action="sitepages.registry_synced").count() == 1

    def test_existing_pages_and_slot_values_are_never_modified(self):
        registry.sync_registry()
        career = Page.objects.get(slug="career")
        career.versioned_update(None, title="Work with us", status="DRAFT")
        PageTextSlot.objects.filter(key="hero_title").update(value="Kept")
        PageTextSlot.objects.filter(key="hero_subtitle").delete()
        assert registry.sync_registry() == {"pages_created": 0, "slots_created": 1, "conflicts": []}
        career.refresh_from_db()
        assert (career.title, career.status) == ("Work with us", "DRAFT") and PageTextSlot.objects.get(key="hero_title").value == "Kept"

    def test_a_slug_held_by_another_page_is_reported_not_fatal(self):
        """``seed_pages`` runs on every release: a registry route whose slug another live page holds (e.g. an imported
        ``/careers`` took ``career``) must be skipped and reported, never abort the whole sync."""
        PageFactory(slug="career", route="/careers")
        out = StringIO()
        call_command("seed_pages", stdout=out)
        assert not Page.objects.filter(route="/career").exists() and Page.objects.count() == 27
        assert "26 page(s) and 0 slot(s) created." in out.getvalue() and "/career" in out.getvalue()
        assert registry.sync_registry() == {"pages_created": 0, "slots_created": 0, "conflicts": ["/career"]}

    @pytest.mark.parametrize(
        "route,slug",
        [
            ("/", "home"),
            ("/solar-warranty", "solar-warranty"),
            ("/career-page", "career"),
            ("/a/b_c", "a-b-c"),
            ("/Upper", "upper"),
            ("/" + "a" * 100, "a" * 80),  # the slug column holds 80 characters; routes hold 255
            ("/" + "a" * 79 + "/b", "a" * 79),  # never ends on a hyphen (slug CHECK)
        ],
    )
    def test_slug_for_route(self, route, slug):
        assert registry.slug_for_route(route) == slug

    def test_a_long_route_is_imported_with_a_cut_slug(self):
        from sitepages.services import legacy_import

        route = "/guides/" + "-".join(["kerala-rooftop-solar"] * 8)
        result = legacy_import.import_pages([{"id": 1, "name": "Long", "route": route, "status": "published", "created_at": None, "updated_at": None}])
        assert result["created"] == 1 and not result["violations"]
        assert len(Page.objects.get(route=route).slug) <= 80


class TestSitemap:
    def test_published_indexable_pages_with_lastmod(self):
        t0 = timezone.now() - dt.timedelta(days=10)
        page = PageFactory(route="/test/a", sort_order=1)
        Page.all_objects.filter(pk=page.pk).update(updated_at=t0)
        noindex = PageFactory(route="/test/noindex", sort_order=2)
        PageSeoFactory(page=noindex, noindex=True)
        indexed = PageFactory(route="/test/indexed", sort_order=3)
        PageSeoFactory(page=indexed, noindex=False)
        PageFactory(route="/test/draft", status="DRAFT")
        PageFactory(route="/test/gone").soft_delete()
        entries = sitemap_entries()
        assert [entry["path"] for entry in entries] == ["/test/a", "/test/indexed"]
        assert entries[0]["lastmod"] == t0

    def test_lastmod_follows_faqs_that_were_ever_published(self):
        t0 = timezone.now() - dt.timedelta(days=10)
        page = PageFactory(route="/test/faq-page")
        from faqs.models import Faq

        Page.all_objects.filter(pk=page.pk).update(updated_at=t0)
        draft = FaqFactory(page=page)
        Faq.all_objects.filter(pk=draft.pk).update(updated_at=t0 + dt.timedelta(days=5))
        assert sitemap_entries()[0]["lastmod"] == t0  # a never-published draft does not change the page
        published = PublishedFaqFactory(page=page)
        Faq.all_objects.filter(pk=published.pk).update(updated_at=t0 + dt.timedelta(days=3))
        assert sitemap_entries()[0]["lastmod"] == t0 + dt.timedelta(days=3)
