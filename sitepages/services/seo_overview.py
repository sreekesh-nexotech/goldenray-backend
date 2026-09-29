"""Maintained pages in the staff SEO overview (convention: ``<app>/services/seo_overview.py``, see ``seo.services``).

Every live page that is not archived; a page without an SEO block counts too (its title falls back to the page title and
its missing description is an error — the rules ``PageSeo.seo_issues`` applies to ``default_seo(page)``).
"""

from __future__ import annotations

from django.db.models import Case, F, When

from seo.services.overview import overview_rows
from sitepages.models import Page

SEO_OVERVIEW_KIND = "page"


def seo_overview_rows():
    return overview_rows(
        Page.objects.exclude(status=Page.Status.ARCHIVED).order_by(),  # no model ordering inside the UNION ALL
        kind=SEO_OVERVIEW_KIND,
        label=F("title"),
        path=F("route"),
        record_status=F("status"),
        seo_title=_live(F("seo__seo_title")),
        meta_description=_live(F("seo__meta_description")),
        fallback_title=F("title"),
        schema_type=_live(F("seo__schema_type")),
        noindex=_live(F("seo__noindex")),
    )


def _live(expression):
    """The SEO block's column, or NULL when the page has none or it is soft-deleted."""
    return Case(When(seo__deleted_at__isnull=True, then=expression), default=None)
