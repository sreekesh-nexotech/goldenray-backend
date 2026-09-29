"""What pricing registers with other registries (called from ``PricingConfig.ready``).

* ``catalog.services.pricing_hooks`` — the website price provider (:mod:`pricing.services.provider`);
* ``catalog.services.usage`` — ``pricing.swap_deltas`` (live swap deltas of non-retired sets) and
  ``pricing.current_release`` (the component is priced in the current PriceRelease): a component referenced there
  cannot be deleted, it must be retired;
* ``core.dashboard`` — counters for the ``pricing`` and ``offers`` modules.
"""

from __future__ import annotations

from django.db.models import Q

from pricing.models import MarketRateSetStatus, Offer, OfferStatus, PriceRelease, PriceReleaseLine, ReleaseStatus, SwapDelta


def swap_delta_usage(component):
    rows = SwapDelta.objects.filter(Q(from_component=component) | Q(to_component=component), set__deleted_at__isnull=True).exclude(set__status=MarketRateSetStatus.RETIRED).select_related("set")
    for row in rows:
        yield {"object_type": "pricing.market_rate_set", "object_uid": row.set.uid, "label": f"{row.set.name}: {row.slot} swap", "status": row.set.status}


def release_usage(component):
    for line in PriceReleaseLine.objects.filter(component=component, release__status=ReleaseStatus.PUBLISHED).select_related("release"):
        yield {"object_type": "pricing.release", "object_uid": line.release.uid, "label": f"PriceRelease #{line.release.number}", "status": line.release.status}


def pricing_counts(user) -> dict[str, int]:
    current = PriceRelease.objects.filter(status=ReleaseStatus.PUBLISHED).values_list("number", flat=True).first()
    from pricing.models import MarketRateSet

    return {
        "current_release": current or 0,
        "draft_market_rate_sets": MarketRateSet.objects.filter(status=MarketRateSetStatus.DRAFT).count(),
    }


def offer_counts(user) -> dict[str, int]:
    offers = Offer.objects.all()
    return {status.lower(): offers.filter(status=status).count() for status in (OfferStatus.DRAFT, OfferStatus.APPROVED, OfferStatus.ACTIVE, OfferStatus.PAUSED)}


def register() -> None:
    from catalog.services import usage
    from core import dashboard
    from pricing.services import provider

    provider.register()
    usage.register("pricing.swap_deltas")(swap_delta_usage)
    usage.register("pricing.current_release")(release_usage)
    dashboard.register("pricing")(pricing_counts)
    dashboard.register("offers")(offer_counts)
