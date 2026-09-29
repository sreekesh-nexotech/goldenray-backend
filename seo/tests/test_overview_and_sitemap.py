"""seo/overview/ (SQL-evaluated status, UNION of providers, paginated) and sitemap/entries/ (convention aggregation)."""

import types

import pytest
from django.utils import timezone

from blog.models import Entry
from blog.tests.factories import CollectionFactory, EntryFactory, EntrySeoFactory, PublishedEntryFactory
from seo.models import seo_issues_for, seo_status_for
from seo.services import overview, providers, sitemap_feed

pytestmark = pytest.mark.django_db
OVERVIEW = "/api/v1/seo/overview/"
SITEMAP = "/api/public/v1/sitemap/entries/"
GOOD_DESCRIPTION = "A description that is long enough to read well in search results for this entry, ok."


@pytest.fixture
def client(auth_client, make_user):
    return auth_client(make_user(grants={"seo": ["view"]}))


class TestOverview:
    def test_permissions(self, api_client, auth_client, make_user):
        assert api_client.get(OVERVIEW).status_code == 401
        assert auth_client(make_user(grants={"blogs": "*"})).get(OVERVIEW).status_code == 403

    def test_sql_status_matches_the_python_rules(self, client):
        cases = [
            ("", "", False),  # no seo block values → falls back to the title; no description → error
            ("Short title", GOOD_DESCRIPTION, False),
            ("T" * 61, GOOD_DESCRIPTION, False),
            ("Title", "too short", False),
            ("Title", "D" * 161, False),
            ("Title", "", True),
            ("   ", GOOD_DESCRIPTION, False),
            # Pasted text often carries newlines/tabs: Python's strip() removes every whitespace character, so must SQL.
            ("Title", "D" * 160 + "\n", False),
            ("Title", "\n\t\r\n", False),
            ("T" * 60 + "\n", GOOD_DESCRIPTION, False),
            ("\t\n", GOOD_DESCRIPTION, False),
        ]
        entries = []
        for index, (title, description, noindex) in enumerate(cases):
            entry = EntryFactory(title=f"Entry {index}")
            EntrySeoFactory(entry=entry, seo_title=title, meta_description=description, noindex=noindex)
            entries.append(entry)
        bare = EntryFactory(title="No SEO block")
        rows = {row["uid"]: row for row in client.get(f"{OVERVIEW}?page_size=50").json()["results"]}
        for entry, (title, description, noindex) in zip(entries, cases):
            row = rows[str(entry.uid)]
            effective = title or entry.title
            assert row["seo_status"] == seo_status_for(effective, description, noindex), (title, description, noindex)
            assert row["issues"] == seo_issues_for(effective, description, noindex)
        assert rows[str(bare.uid)]["seo_status"] == "error" and rows[str(bare.uid)]["path"] == f"{bare.collection.path_prefix}/{bare.slug}"

    def test_worst_first_filters_counts_and_pagination(self, client):
        ok = PublishedEntryFactory(title="Fine")
        EntrySeoFactory(entry=ok, seo_title="Fine", meta_description=GOOD_DESCRIPTION)
        EntryFactory.create_batch(3)  # errors
        EntryFactory(status=Entry.Status.ARCHIVED, archived_at=timezone.now())  # never listed
        body = client.get(f"{OVERVIEW}?page_size=2").json()
        assert body["count"] == 4 and len(body["results"]) == 2 and body["next"] and body["kinds"] == ["blog"]
        assert body["counts"] == {"error": 3, "warning": 0, "ok": 1}
        assert [row["seo_status"] for row in client.get(OVERVIEW).json()["results"]] == ["error", "error", "error", "ok"]
        assert [row["uid"] for row in client.get(f"{OVERVIEW}?seo_status=ok").json()["results"]] == [str(ok.uid)]
        assert client.get(f"{OVERVIEW}?kind=page").json() == {"results": [], "count": 0, "next": None, "previous": None, "counts": {"error": 0, "warning": 0, "ok": 0}, "kinds": ["blog"]}
        assert client.get(f"{OVERVIEW}?seo_status=bad").status_code == 400

    def test_soft_deleted_seo_block_counts_as_missing(self, client):
        seo = EntrySeoFactory(meta_description=GOOD_DESCRIPTION)
        seo.soft_delete()
        assert client.get(OVERVIEW).json()["results"][0]["meta_description"] == ""

    def test_providers_are_combined_with_union_all(self, client, monkeypatch):
        from blog.services import seo_overview as blog_rows

        other = types.SimpleNamespace(SEO_OVERVIEW_KIND="page", seo_overview_rows=lambda: blog_rows.seo_overview_rows().filter(o_label__startswith="Page"))
        monkeypatch.setattr(overview, "overview_providers", lambda: (blog_rows, other))
        EntryFactory(title="Page-like")
        EntryFactory(title="Article")
        body = client.get(OVERVIEW).json()
        assert body["count"] == 3 and body["counts"]["error"] == 3 and sorted(body["kinds"]) == ["blog", "page"]
        assert client.get(f"{OVERVIEW}?kind=page").json()["count"] == 1

    def test_query_count_is_bounded(self, client, django_assert_max_num_queries):
        EntryFactory.create_batch(3)
        with django_assert_max_num_queries(12) as small:
            client.get(OVERVIEW)
        EntryFactory.create_batch(20)
        with django_assert_max_num_queries(len(small.captured_queries)):
            assert client.get(OVERVIEW).json()["count"] == 23


class TestSitemap:
    def test_blog_entries_and_collection_index(self, api_client):
        collection = CollectionFactory(path_prefix="/blog")
        visible = PublishedEntryFactory(collection=collection, slug="visible-one")
        hidden = PublishedEntryFactory(collection=collection, slug="hidden-one")
        EntrySeoFactory(entry=hidden, noindex=True)
        indexed = PublishedEntryFactory(collection=collection, slug="with-seo")
        EntrySeoFactory(entry=indexed, noindex=False)
        EntryFactory(collection=collection, slug="draft-one")
        PublishedEntryFactory(collection=CollectionFactory(is_active=False, path_prefix="/closed"), slug="closed-one")
        response = api_client.get(SITEMAP)
        assert response.status_code == 200 and response["Cache-Control"] == "public, max-age=60"
        paths = [row["path"] for row in response.json()["results"]]
        assert paths == ["/blog", "/blog/visible-one", "/blog/with-seo"]
        lastmod = {row["path"]: row["lastmod"] for row in response.json()["results"]}
        visible.refresh_from_db()
        assert lastmod["/blog/visible-one"] == visible.updated_at.isoformat()

    def test_blog_writes_invalidate_the_cached_sitemap(self, api_client, auth_client, make_user):
        entry = EntryFactory(collection=CollectionFactory(path_prefix="/blog"), slug="soon-live")
        assert api_client.get(SITEMAP).json()["count"] == 0
        assert api_client.get(SITEMAP)["X-Cache"] == "HIT"
        auth_client(make_user(grants={"blogs": "*"})).post(f"/api/v1/content/entries/{entry.uid}/publish/", {}, format="json")
        again = api_client.get(SITEMAP)
        assert again["X-Cache"] == "MISS" and again.json()["count"] == 2

    def test_aggregation_rules(self, monkeypatch):
        first = types.SimpleNamespace(
            __name__="one", SITEMAP_CACHE_NAMESPACES=("one:ns",), sitemap_entries=lambda: [{"path": "/b", "lastmod": "2026-01-01"}, {"path": "/a", "lastmod": None}, {"path": "bad"}, "junk"]
        )
        second = types.SimpleNamespace(__name__="two", sitemap_entries=lambda: [{"path": "/b", "lastmod": timezone.datetime(2026, 2, 1).date()}])
        monkeypatch.setattr(sitemap_feed, "sitemap_providers", lambda: (first, second))
        assert sitemap_feed.entries() == [{"path": "/a", "lastmod": None}, {"path": "/b", "lastmod": "2026-02-01"}]
        assert sitemap_feed.cache_namespaces() == ["seo:sitemap", "one:ns"]

    def test_discovery_finds_providers_only_where_the_module_exists(self):
        # every integrated website app that ships ``services/sitemap.py`` — and nothing else
        assert [module.__name__ for module in providers.sitemap_providers()] == ["blog.services.sitemap", "sitepages.services.sitemap", "careers.services.sitemap"]
        assert [module.__name__ for module in providers.overview_providers()] == ["blog.services.seo_overview"]

    def test_query_count_is_bounded(self, api_client, django_assert_max_num_queries):
        collection = CollectionFactory()
        PublishedEntryFactory.create_batch(3, collection=collection)
        with django_assert_max_num_queries(4):
            api_client.get(SITEMAP)
        PublishedEntryFactory.create_batch(20, collection=collection)
        with django_assert_max_num_queries(4):
            assert api_client.get(f"{SITEMAP}?page_size=100").json()["count"] == 24
