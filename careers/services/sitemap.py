"""Sitemap entries for the careers pages (PLAN §3.3 ``sitemap/entries``: slugs + lastmod for ``next-sitemap``).

Published postings only (a closed posting still renders, but it no longer belongs in a sitemap), minus those marked
``noindex``. The ``seo`` package's ``sitemap/entries`` endpoint aggregates these per context.
"""

from __future__ import annotations

from careers.models import JobPosition
from careers.services.positions import CACHE_NAMESPACE

#: The cached ``sitemap/entries/`` depends on the positions (every position write bumps this namespace).
SITEMAP_CACHE_NAMESPACES = (CACHE_NAMESPACE,)


def sitemap_entries() -> list[dict]:
    """``[{"path": "/career/<slug>", "lastmod": <ISO date-time>, "kind": "job_position"}]``, in display order."""
    rows = (
        JobPosition.objects.filter(status=JobPosition.Status.PUBLISHED, noindex=False)
        .order_by("sort_order", "-published_at", "-created_at", "id")
        .only("slug", "updated_at", "published_at", "sort_order", "created_at")
    )
    return [{"path": f"/career/{row.slug}", "lastmod": row.updated_at.isoformat(), "kind": "job_position"} for row in rows]
