"""Sitemap contribution of the maintained pages (aggregated by the SEO package's ``sitemap/entries``).

Convention: every content app exposes ``<app>.services.sitemap.sitemap_entries() -> [{"path": str, "lastmod":
datetime}]``. FAQs are not addressable on their own (they render on their page), so the ``faqs`` app contributes
nothing; instead a page's ``lastmod`` is the latest of its own last content change (the page aggregate's
``updated_at``: every slot/SEO edit touches it) and the last change of any FAQ on it that has ever been published.
Only PUBLISHED pages that are not ``noindex`` are listed.
"""

from __future__ import annotations

from django.db.models import Max, Q

from sitepages.models import Page
from sitepages.services.pages import CACHE_NAMESPACE

#: The cached ``sitemap/entries/`` depends on pages and on the FAQs whose changes move a page's ``lastmod`` (``faqs`` is
#: the faqs app's namespace; sitepages sits below faqs and never imports it).
SITEMAP_CACHE_NAMESPACES = (CACHE_NAMESPACE, "faqs")


def sitemap_entries() -> list[dict]:
    rows = (
        Page.objects.filter(status=Page.Status.PUBLISHED)
        .exclude(seo__noindex=True)
        # Reverse relation of faqs.Faq.page (no import: sitepages sits below faqs).
        .annotate(faq_lastmod=Max("faqs__updated_at", filter=Q(faqs__deleted_at__isnull=True, faqs__published_at__isnull=False)))
        .order_by("sort_order", "route")
        .values_list("route", "updated_at", "faq_lastmod")
    )
    return [{"path": route, "lastmod": max(stamp for stamp in (updated_at, faq_lastmod) if stamp is not None)} for route, updated_at, faq_lastmod in rows]
