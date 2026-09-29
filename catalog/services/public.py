"""Reads behind ``/api/public/<version>/products/…`` (PLAN §3.3): published profiles + spec + price range.

A product is on the website while its profile is PUBLISHED, its component is live, public (``is_public``) and ACTIVE
or DEPRECATED, and its category is live and active. ``products/panels/``, ``products/inverters/`` and
``products/batteries/`` list the categories ``panel``, ``inverter`` and ``battery``; ``products/<category>/<slug>/``
accepts a category slug or one of those three aliases.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.db.models import F

from catalog.models import ComponentPublicProfile, ProfileStatus
from catalog.services import pricing_hooks
from catalog.services.profiles import PUBLISHABLE_STATUSES
from core.errors import NotFound
from media.models import MediaAsset

CATEGORY_ALIASES = {"panels": "panel", "inverters": "inverter", "batteries": "battery"}


def resolve_category(category: str) -> str:
    return CATEGORY_ALIASES.get(category, category)


def published_profiles():
    return ComponentPublicProfile.objects.filter(
        status=ProfileStatus.PUBLISHED,
        component__deleted_at__isnull=True,
        component__is_public=True,
        component__status__in=PUBLISHABLE_STATUSES,
        component__category__deleted_at__isnull=True,
        component__category__is_active=True,
    ).select_related(
        "component__category",
        "component__brand",
        "component__primary_image",
        "component__datasheet",
        "component__panel_spec",
        "component__inverter_spec",
        "component__battery_spec__family",
        "component__structure_spec",
    )


def products_in(category: str):
    return published_profiles().filter(component__category__slug=resolve_category(category)).order_by(F("kerala_climate_score").desc(nulls_last=True), "slug", "id")


def get_product(category: str, slug: str) -> ComponentPublicProfile:
    profile = published_profiles().filter(component__category__slug=resolve_category(category), slug=slug).first()
    if profile is None:
        raise NotFound("product_not_found", "No published product with this slug in this category.")
    return profile


def price_context(profiles: Sequence[ComponentPublicProfile]) -> dict:
    """``{component pk: PriceInfo | None}`` for a page of profiles — one provider call per response."""
    return pricing_hooks.prices_for([profile.component for profile in profiles])


def gallery_assets(profile: ComponentPublicProfile) -> list[MediaAsset]:
    """The profile's gallery as live public images, in the stored order (deleted/private entries are skipped)."""
    uids = [uid for uid in profile.gallery or [] if isinstance(uid, str)]
    if not uids:
        return []
    assets = {str(asset.uid): asset for asset in MediaAsset.objects.filter(uid__in=uids, visibility=MediaAsset.Visibility.PUBLIC)}
    return [assets[uid] for uid in uids if uid in assets]
