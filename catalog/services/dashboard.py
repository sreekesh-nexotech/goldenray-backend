"""Catalog counters for ``GET /api/<version>/dashboard/`` (registered from ``CatalogConfig.ready``).

Both modules support only the ``all`` scope, so the counters apply ``core.scopes`` for completeness (fail closed).
"""

from __future__ import annotations

from django.db.models import Count, Q

from catalog.models import Brand, Category, Component, ComponentPublicProfile, ComponentStatus, ProfileStatus
from core import scopes


def catalog_counts(user) -> dict[str, int]:
    components = scopes.apply(Component.objects.all(), user, "catalog")
    totals = components.aggregate(
        components=Count("id"),
        draft=Count("id", filter=Q(status=ComponentStatus.DRAFT)),
        active=Count("id", filter=Q(status=ComponentStatus.ACTIVE)),
        deprecated=Count("id", filter=Q(status=ComponentStatus.DEPRECATED)),
        retired=Count("id", filter=Q(status=ComponentStatus.RETIRED)),
    )
    totals["brands"] = scopes.apply(Brand.objects.all(), user, "catalog").count()
    totals["categories"] = scopes.apply(Category.objects.all(), user, "catalog").count()
    return totals


def public_profile_counts(user) -> dict[str, int]:
    profiles = scopes.apply(ComponentPublicProfile.objects.filter(component__deleted_at__isnull=True), user, "products_public")
    return profiles.aggregate(
        profiles=Count("id"),
        published=Count("id", filter=Q(status=ProfileStatus.PUBLISHED)),
        draft=Count("id", filter=Q(status=ProfileStatus.DRAFT)),
    )


def register() -> None:
    from core import dashboard

    dashboard.register("catalog")(catalog_counts)
    dashboard.register("products_public")(public_profile_counts)
