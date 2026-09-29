"""Blog's contribution to the public ``sitemap/entries/`` (convention: ``<app>/services/sitemap.py``, see seo).

Published entries of active collections that are not ``noindex``, at ``<collection path_prefix>/<slug>``, plus each
active collection's index page (``lastmod`` = its newest entry). ``SITEMAP_CACHE_NAMESPACES`` lists what the result
depends on, so the aggregated, cached sitemap is invalidated by blog writes.
"""

from __future__ import annotations

from django.db.models import Max, Q

from blog.models import Entry
from blog.services.common import NS_COLLECTIONS, NS_ENTRIES

SITEMAP_CACHE_NAMESPACES = (NS_ENTRIES, NS_COLLECTIONS)


def _visible():
    no_seo_block = Q(seo__isnull=True) | Q(seo__deleted_at__isnull=False)
    return Entry.objects.filter(status=Entry.Status.PUBLISHED, collection__is_active=True, collection__deleted_at__isnull=True).filter(no_seo_block | Q(seo__noindex=False))


def sitemap_entries():
    entries = _visible().values_list("collection__path_prefix", "slug", "updated_at").order_by("collection__path_prefix", "slug")
    for prefix, slug, updated_at in entries:
        yield {"path": f"{prefix}/{slug}", "lastmod": updated_at}
    for prefix, lastmod in _visible().values("collection__path_prefix").annotate(lastmod=Max("updated_at")).values_list("collection__path_prefix", "lastmod").order_by("collection__path_prefix"):
        yield {"path": prefix, "lastmod": lastmod}
