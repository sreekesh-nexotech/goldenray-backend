"""Public ``sitemap/entries/`` — slugs + lastmod for ``next-sitemap`` (PLAN §3.3, §6.5 #5), aggregated by convention.

Every installed app exposing ``<app>/services/sitemap.py`` with ``sitemap_entries() -> iterable of {path, lastmod}``
is included (blog today; pages and careers add theirs). A provider may declare ``SITEMAP_CACHE_NAMESPACES`` — the
cached response depends on all of them. Paths must be site-relative; duplicates keep the newest ``lastmod``; the
result is sorted by path so pagination is stable.
"""

from __future__ import annotations

import datetime as dt
import logging

from seo.services.providers import sitemap_providers

logger = logging.getLogger("flarize.seo")
BASE_NAMESPACES = ("seo:sitemap",)


def cache_namespaces() -> list[str]:
    names = list(BASE_NAMESPACES)
    for provider in sitemap_providers():
        for name in getattr(provider, "SITEMAP_CACHE_NAMESPACES", ()):
            if name not in names:
                names.append(name)
    return names


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def entries() -> list[dict]:
    """``[{"path", "lastmod"}]`` from every provider, sorted by path."""
    merged: dict[str, dict] = {}
    for provider in sitemap_providers():
        for item in provider.sitemap_entries():
            path = item.get("path") if isinstance(item, dict) else None
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                logger.warning("sitemap provider returned an invalid path", extra={"provider": provider.__name__, "path": str(path)[:200]})
                continue
            lastmod = _iso(item.get("lastmod"))
            current = merged.get(path)
            if current is None or (lastmod or "") > (current["lastmod"] or ""):
                merged[path] = {"path": path, "lastmod": lastmod}
    return [merged[path] for path in sorted(merged)]
