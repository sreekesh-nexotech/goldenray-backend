"""GET procurement/price-master/ — current purchase/landed per component with the batch that set them."""

from decimal import Decimal

import pytest
from django.db import transaction

from catalog.tests.factories import CategoryFactory, ComponentFactory
from pricing.models import PriceKind, PriceSource
from pricing.services.prices import write_price
from procurement.tests.factories import BatchChargeFactory, BatchFactory, BatchLineFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/procurement/price-master/"


def committed(client, count=3):
    batch = BatchFactory()
    for index in range(count):
        BatchLineFactory(batch=batch, component=ComponentFactory(sku=f"m{index}"), unit_purchase_price=Decimal("100") + index)
    BatchChargeFactory(batch=batch, amount=Decimal("30"))
    assert client.post(f"/api/v1/procurement/batches/{batch.uid}/commit/", {"reason": "seed"}, format="json").status_code == 200
    return batch


def test_permissions(api_client, outsider, viewer):
    assert api_client.get(URL).status_code == 401
    assert outsider.get(URL).status_code == 403
    assert viewer.get(URL).status_code == 200


def test_entries_with_batch_refs_and_internal_gating(client, viewer):
    batch = committed(client)
    body = client.get(URL).json()
    assert body["count"] == 3
    entry = body["results"][0]
    assert entry["component"]["sku"] == "m0" and entry["purchase_price"] == "100.00" and entry["landed_unit_cost"] == "100.00"
    assert entry["batch"]["number"] == batch.number and entry["supplier"]["code"] == batch.supplier.code
    assert entry["purchase_version_key"] == f"{batch.number}::m0"
    hidden = viewer.get(URL).json()["results"][0]
    assert "landed_unit_cost" not in hidden and hidden["purchase_price"] == "100.00"


def test_filters_and_manual_rows(client):
    committed(client, count=2)
    other = ComponentFactory(sku="x9", category=CategoryFactory(slug="special"))
    with transaction.atomic():
        write_price(other, PriceKind.LANDED, Decimal("55"), user=None, source=PriceSource.MANUAL)
    manual = client.get(URL, {"search": "x9"}).json()["results"][0]
    assert manual["batch"] is None and manual["purchase_price"] is None and manual["landed_unit_cost"] == "55.00"
    assert client.get(URL, {"category": "special"}).json()["count"] == 1
    assert ComponentFactory(sku="noprice") and client.get(URL, {"search": "noprice"}).json()["count"] == 0


def test_query_budget(client, django_assert_max_num_queries):
    committed(client, count=20)
    with django_assert_max_num_queries(10):
        assert client.get(URL, {"page_size": 50}).json()["count"] == 20
