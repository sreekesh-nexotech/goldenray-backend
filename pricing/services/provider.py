"""The catalog price provider (``catalog.services.pricing_hooks``): website product prices from the current release.

Registered from ``PricingConfig.ready()``. Called once per public product response with every component on the page
(two queries: the current release, its lines for those components). A component's price is its release line's list
price made customer-facing (GST added unless the list price already includes it; whole rupees, ``money.js``
rounding); the label is that amount in Indian grouping (``₹13,585``). A component without a positive list price in the
current release gets no price, so the website falls back to the profile's hand-written range. Public product
responses are cached under the ``pricing`` namespace, which every release publish bumps.
"""

from __future__ import annotations

from catalog.services import pricing_hooks
from pricing.models import PriceReleaseLine, ReleaseStatus
from pricing.services.releases import gst_inclusive_list_price


def current_release_prices(components) -> dict:
    by_pk = {component.pk: component for component in components}
    lines = PriceReleaseLine.objects.filter(release__status=ReleaseStatus.PUBLISHED, component_id__in=list(by_pk)).select_related("release")
    result = {}
    for line in lines:
        amount = gst_inclusive_list_price(line)
        if amount is None:
            continue
        info = pricing_hooks.PriceInfo(min_amount=amount, max_amount=amount, currency="INR", gst_inclusive=True, release_number=line.release.number)
        result[line.component_id] = pricing_hooks.PriceInfo(
            min_amount=amount, max_amount=amount, currency="INR", gst_inclusive=True, label=pricing_hooks.price_label(info), release_number=line.release.number
        )
    return result


def register() -> None:
    pricing_hooks.register(current_release_prices)
