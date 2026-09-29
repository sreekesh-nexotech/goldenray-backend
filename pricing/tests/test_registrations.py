"""What pricing and procurement register elsewhere: catalog usage (delete guard), dashboard counters, price provider."""

from decimal import Decimal

import pytest

from catalog.services import usage
from catalog.tests.factories import ComponentFactory
from core import dashboard
from pricing.models import PriceKind, PriceRelease, PriceReleaseLine
from pricing.services import provider
from pricing.tests.factories import MarketRateSetFactory, OfferFactory, SwapDeltaFactory
from procurement.tests.factories import BatchFactory, BatchLineFactory

pytestmark = pytest.mark.django_db


def release_with(component, **line) -> PriceRelease:
    release = PriceRelease.objects.create(number=1, published_at="2026-09-01T00:00:00Z", market_rate_set=MarketRateSetFactory(), payload={}, payload_sha256="0" * 64)
    PriceReleaseLine.objects.create(release=release, component=component, gst_rate=Decimal("0.18"), **line)
    return release


def test_components_in_use_cannot_be_deleted(auth_client, make_user):
    client = auth_client(make_user(grants={"catalog": "*"}))
    swapped, released, drafted = ComponentFactory(), ComponentFactory(), ComponentFactory()
    SwapDeltaFactory(from_component=swapped)
    release_with(released, list_price=Decimal("100"))
    BatchLineFactory(component=drafted)
    for component, section in ((swapped, "pricing.swap_deltas"), (released, "pricing.current_release"), (drafted, "procurement.draft_batches")):
        body = client.get(f"/api/v1/catalog/components/{component.uid}/usage/").json()
        assert {s["name"]: s["count"] for s in body["sections"]}[section] == 1
        response = client.delete(f"/api/v1/catalog/components/{component.uid}/")
        assert response.status_code == 409 and section in response.json()["errors"]
    assert {"pricing.swap_deltas", "pricing.current_release", "procurement.draft_batches"} <= set(usage.registered())


def test_dashboard_counters(make_user):
    user = make_user(grants={"pricing": ["view"], "offers": ["view"], "procurement": ["view"]})
    MarketRateSetFactory()
    OfferFactory(status="ACTIVE")
    BatchFactory()
    counters = dashboard.counters_for(user)
    assert counters["pricing"] == {"current_release": 0, "draft_market_rate_sets": 1}
    assert counters["offers"]["active"] == 1 and counters["procurement"]["draft_batches"] == 1


def test_provider_prices(make_user):
    included = ComponentFactory()
    zero = ComponentFactory()
    unlisted = ComponentFactory()
    release = release_with(included, list_price=Decimal("1000.00"), list_price_gst_inclusive=True)
    PriceReleaseLine.objects.create(release=release, component=zero, list_price=Decimal("0"), gst_rate=Decimal("0.05"))
    prices = provider.current_release_prices([included, zero, unlisted])
    assert set(prices) == {included.pk}
    assert prices[included.pk].min_amount == Decimal("1000.00") and prices[included.pk].label == "₹1,000" and prices[included.pk].release_number == 1
    assert PriceKind.LIST == "LIST"
