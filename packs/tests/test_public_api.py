"""``/api/public/v1/packs/`` and ``packs/<system_type>/<tier>/<size_kw>/`` — shape (no cost fields), cache, throttle."""

from __future__ import annotations

import pytest

from packs.services import releases
from packs.views.public import PublicPackDetailView, PublicPackListView

pytestmark = pytest.mark.django_db
BASE = "/api/public/v1/packs/"
FORBIDDEN = {"landed_cost_total", "gross_margin_pct", "pricing", "internal", "unit_price", "amount"}


@pytest.fixture
def released(world):
    return releases.publish(user=None)


def _keys(value) -> set:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values())) if value else set(value)
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


def test_list_shape_and_no_cost_fields(api_client, released, django_assert_max_num_queries):
    with django_assert_max_num_queries(6):
        response = api_client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    pack = body["results"][0]
    assert pack["key"] == "ongrid-value-3" and pack["customer_price_incl_gst"] == "229000.00" and pack["release_number"] == 1
    assert [line["category"] for line in pack["bom"]] == ["panel", "inverter", "isolator", "fixed"]
    assert not (_keys(body) & FORBIDDEN)


def test_list_filters(api_client, released):
    assert api_client.get(BASE, {"system_type": "HYBRID"}).json()["count"] == 0
    assert api_client.get(BASE, {"future_ready": "true"}).json()["count"] == 0
    assert api_client.get(BASE, {"future_ready": "false"}).json()["count"] == 1
    assert api_client.get(BASE, {"tier": "NOPE"}).status_code == 400


def test_empty_without_a_release(api_client, world):
    assert api_client.get(BASE).json()["count"] == 0


def test_detail(api_client, released):
    body = api_client.get(f"{BASE}ongrid/value/3/").json()
    assert body["system_type"] == "ONGRID" and body["tier"] == "VALUE" and [p["key"] for p in body["packs"]] == ["ongrid-value-3"]
    assert not (_keys(body) & FORBIDDEN)
    assert api_client.get(f"{BASE}ONGRID/VALUE/3.00/").status_code == 200
    assert api_client.get(f"{BASE}ongrid/value/5/").json()["code"] == "pack_not_found"
    bad = api_client.get(f"{BASE}offgrid/value/3/")
    assert bad.status_code == 400 and "system_type" in bad.json()["errors"]
    assert api_client.get(f"{BASE}ongrid/gold/3/").status_code == 400
    assert api_client.get(f"{BASE}ongrid/value/three/").status_code == 400
    assert api_client.get(f"{BASE}ongrid/value/5sp/").status_code == 404


def test_cache_headers_and_invalidation(api_client, released, world):
    first = api_client.get(BASE)
    assert first["Cache-Control"] == "public, max-age=60" and first["X-Cache"] == "MISS" and first["ETag"]
    assert api_client.get(BASE)["X-Cache"] == "HIT"
    assert api_client.get(BASE, HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304
    world["price_release"].payload["market_rates_by_key"]["ongrid_value"]["3"] = "239000"
    world["price_release"].save()
    releases.publish(user=None)
    fresh = api_client.get(BASE)
    assert fresh["X-Cache"] == "MISS" and fresh.json()["results"][0]["customer_price_incl_gst"] == "239000.00"


def test_throttle_scope():
    request = type("R", (), {"method": "GET"})()
    assert PublicPackListView().get_throttle_scope(request) == "public_read"
    assert PublicPackDetailView().get_throttle_scope(request) == "public_read"
    assert PublicPackListView.authentication_classes == []
