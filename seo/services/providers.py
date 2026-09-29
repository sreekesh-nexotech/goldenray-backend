"""Convention-based discovery of per-app SEO contributions (no imports of other apps' services by name).

* ``<app>/services/sitemap.py`` exposing ``sitemap_entries() -> iterable of {"path", "lastmod"}`` (and optionally
  ``SITEMAP_CACHE_NAMESPACES``) contributes to the public ``sitemap/entries/``;
* ``<app>/services/seo_overview.py`` exposing ``seo_overview_rows() -> QuerySet`` (built with
  :func:`seo.services.overview.overview_rows`) contributes to the staff ``seo/overview/``.

Every installed app is inspected once per process; an app without the module simply contributes nothing.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import import_module
from types import ModuleType

from django.apps import apps


@lru_cache(maxsize=None)
def discover(module: str, attribute: str) -> tuple[ModuleType, ...]:
    found = []
    for config in apps.get_app_configs():
        name = f"{config.name}.services.{module}"
        try:
            candidate = import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name in (name, f"{config.name}.services"):
                continue
            raise
        if callable(getattr(candidate, attribute, None)):
            found.append(candidate)
    return tuple(found)


def sitemap_providers() -> tuple[ModuleType, ...]:
    return discover("sitemap", "sitemap_entries")


def overview_providers() -> tuple[ModuleType, ...]:
    return discover("seo_overview", "seo_overview_rows")
