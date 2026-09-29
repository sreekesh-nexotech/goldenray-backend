"""Blog's rows in the staff SEO overview (convention: ``<app>/services/seo_overview.py``, see ``seo.services``).

Every live, non-archived entry — an entry without an SEO block counts too (its title falls back to the entry title
and its missing description is an error, exactly as ``SeoFields.seo_issues`` rules).
"""

from __future__ import annotations

from django.db.models import Case, F, Value, When
from django.db.models.functions import Concat

from blog.models import Entry
from seo.services.overview import overview_rows

SEO_OVERVIEW_KIND = "blog"


def seo_overview_rows():
    live_seo = {"seo__deleted_at__isnull": True}
    entries = Entry.objects.exclude(status=Entry.Status.ARCHIVED).filter(collection__deleted_at__isnull=True)
    return overview_rows(
        entries,
        kind=SEO_OVERVIEW_KIND,
        label=F("title"),
        path=Concat(F("collection__path_prefix"), Value("/"), F("slug")),
        record_status=F("status"),
        seo_title=_live(F("seo__seo_title"), live_seo),
        meta_description=_live(F("seo__meta_description"), live_seo),
        fallback_title=F("title"),
        schema_type=_live(F("seo__schema_type"), live_seo),
        noindex=_live(F("seo__noindex"), live_seo),
    )


def _live(expression, condition):
    """The SEO block's column, or NULL when the block is soft-deleted."""
    return Case(When(**condition, then=expression), default=None)
