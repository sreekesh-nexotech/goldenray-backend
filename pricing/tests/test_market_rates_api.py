"""pricing/market-rate-sets/ — sets, bulk PUT of rates / swap deltas / roof add-ons, activation (exactly one ACTIVE)."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from audit.models import AuditLog
from catalog.tests.factories import ComponentFactory
from core.models import OutboxEvent
from pricing.models import MarketRate, MarketRateSet, MarketRateSetStatus
from pricing.services.market_rates import rate_key
from pricing.tests.factories import MarketRateFactory, MarketRateSetFactory, RoofAddonFactory, SwapDeltaFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/pricing/market-rate-sets/"


def detail(rate_set, suffix=""):
    return f"{URL}{rate_set.uid}/{suffix}"


RATES = [
    {"system_type": "ONGRID", "tier": "VALUE", "size_key": "3", "customer_price_incl_gst": "229000"},
    {"system_type": "ONGRID", "tier": "VALUE", "size_key": "5sp", "customer_price_incl_gst": "330000"},
    {"system_type": "ONGRID", "tier": "VALUE", "size_key": "5tp", "customer_price_incl_gst": "350000"},
    {"system_type": "HYBRID", "tier": "BASE", "battery_config": "2", "size_key": "3", "customer_price_incl_gst": "0"},
    {"system_type": "ONGRID", "tier": "VALUE", "size_key": "3", "future_size_key": "5sp", "customer_price_incl_gst": "245000"},
    {"system_type": "UPGRADE", "size_key": "5", "from_size_key": "3", "customer_price_incl_gst": "90000"},
    {"system_type": "HYBRID", "tier": "VALUE", "battery_config": "2", "size_key": "3", "variant": "diffBase", "customer_price_incl_gst": "0"},
]


class TestPermissions:
    def test_anonymous_and_missing_permission(self, api_client, outsider, viewer):
        rate_set = MarketRateSetFactory()
        assert api_client.get(URL).status_code == 401
        assert outsider.get(URL).status_code == 403
        assert viewer.get(URL).status_code == 200
        assert viewer.get(detail(rate_set, "rates/")).status_code == 200
        assert viewer.post(URL, {"name": "x"}, format="json").status_code == 403
        assert viewer.put(detail(rate_set, "rates/"), {"rates": []}, format="json").status_code == 403
        assert viewer.post(detail(rate_set, "activate/"), {}, format="json").status_code == 403

    def test_activation_needs_publish(self, editor):
        rate_set = MarketRateSetFactory()
        MarketRateFactory(set=rate_set)
        response = editor.post(detail(rate_set, "activate/"), {}, format="json")
        assert response.status_code == 403


class TestSets:
    def test_create_put_rates_and_activate(self, client, pricing_user):
        created = client.post(URL, {"name": "Q3 2026", "note": "monsoon"}, format="json")
        assert created.status_code == 201 and created.json()["status"] == "DRAFT"
        rate_set = MarketRateSet.objects.get(uid=created.json()["uid"])
        response = client.put(detail(rate_set, "rates/"), {"rates": RATES, "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["outcome"] == {"created": 7, "updated": 0, "unchanged": 0, "deleted": 0} and response.json()["set"]["rate_count"] == 7
        keys = {(row["key"], row["size_key"]) for row in client.get(detail(rate_set, "rates/"), {"page_size": 50}).json()["results"]}
        assert keys == {
            ("ongrid_value", "3"),
            ("ongrid_value", "5sp"),
            ("ongrid_value", "5tp"),
            ("hybrid_base_2", "3"),
            ("ongrid_value_up5sp", "3"),
            ("upgrade_3_5", "5"),
            ("hybrid_value_2_diffBase", "3"),
        }
        five = MarketRate.objects.filter(set=rate_set, size_key__in=["5sp", "5tp"]).order_by("size_key")
        assert [(r.size_kw, r.phase) for r in five] == [(Decimal("5.00"), "1P"), (Decimal("5.00"), "3P")]
        second = client.put(detail(rate_set, "rates/"), {"rates": RATES[:2] + [{**RATES[2], "customer_price_incl_gst": "360000"}], "expected_version": 2}, format="json")
        assert second.json()["outcome"] == {"created": 0, "updated": 1, "unchanged": 2, "deleted": 4}
        stale = client.put(detail(rate_set, "rates/"), {"rates": RATES, "expected_version": 2}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        activated = client.post(detail(rate_set, "activate/"), {"expected_version": 3}, format="json")
        assert activated.status_code == 200 and activated.json()["status"] == "ACTIVE"
        assert OutboxEvent.objects.filter(event_type="pricing.market_rate_set_activated").count() == 1
        locked = client.put(detail(rate_set, "rates/"), {"rates": RATES}, format="json")
        assert locked.status_code == 409 and locked.json()["code"] == "market_rate_set_not_draft"
        assert AuditLog.objects.filter(action="pricing.market_rate_set_activated", actor=pricing_user).exists()

    @pytest.mark.parametrize(
        "row,field",
        [
            ({"system_type": "ONGRID", "tier": "VALUE", "size_key": "abc", "customer_price_incl_gst": "1"}, "rates[0].size_key"),
            ({"system_type": "ONGRID", "size_key": "3", "customer_price_incl_gst": "1"}, "rates[0].tier"),
            ({"system_type": "HYBRID", "tier": "BASE", "size_key": "3", "customer_price_incl_gst": "1"}, "rates[0].battery_config"),
            ({"system_type": "ONGRID", "tier": "BASE", "battery_config": "1", "size_key": "3", "customer_price_incl_gst": "1"}, "rates[0].battery_config"),
            ({"system_type": "UPGRADE", "size_key": "5", "customer_price_incl_gst": "1"}, "rates[0].from_size_key"),
            ({"system_type": "ONGRID", "tier": "BASE", "size_key": "3", "from_size_key": "3", "customer_price_incl_gst": "1"}, "rates[0].from_size_key"),
            ({"system_type": "ONGRID", "tier": "BASE", "size_key": "3", "variant": "bad-variant", "customer_price_incl_gst": "1"}, "rates[0].variant"),
        ],
    )
    def test_rate_validation(self, client, row, field):
        rate_set = MarketRateSetFactory()
        response = client.put(detail(rate_set, "rates/"), {"rates": [row]}, format="json")
        assert response.status_code == 400 and field in response.json()["errors"]

    def test_duplicate_rows_are_refused(self, client):
        rate_set = MarketRateSetFactory()
        response = client.put(detail(rate_set, "rates/"), {"rates": [RATES[0], RATES[0]]}, format="json")
        assert response.status_code == 400

    def test_exactly_one_active_set(self, client):
        first, second = MarketRateSetFactory(), MarketRateSetFactory()
        MarketRateFactory(set=first)
        MarketRateFactory(set=second)
        assert client.post(detail(first, "activate/"), {}, format="json").status_code == 200
        assert client.post(detail(second, "activate/"), {}, format="json").status_code == 200
        first.refresh_from_db()
        assert first.status == MarketRateSetStatus.RETIRED and first.retired_at is not None
        again = client.post(detail(second, "activate/"), {}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "market_rate_set_already_active"
        assert client.post(detail(first, "activate/"), {}, format="json").status_code == 200  # rollback to a retired set
        with pytest.raises(IntegrityError), transaction.atomic():
            MarketRateSet.objects.filter(pk=second.pk).update(status="ACTIVE")

    def test_empty_set_cannot_activate_and_delete_rules(self, client):
        rate_set = MarketRateSetFactory()
        empty = client.post(detail(rate_set, "activate/"), {}, format="json")
        assert empty.status_code == 409 and empty.json()["code"] == "market_rate_set_empty"
        active = MarketRateSetFactory(status="ACTIVE")
        refused = client.delete(detail(active))
        assert refused.status_code == 409 and refused.json()["code"] == "market_rate_set_active"
        assert client.delete(detail(rate_set)).status_code == 204

    def test_copy_from_and_rename(self, client):
        source = MarketRateSetFactory(status="ACTIVE")
        MarketRateFactory(set=source)
        SwapDeltaFactory(set=source)
        RoofAddonFactory(set=source)
        response = client.post(URL, {"name": "Copy", "copy_from_uid": str(source.uid)}, format="json")
        assert response.status_code == 201
        copy = MarketRateSet.objects.get(uid=response.json()["uid"])
        assert copy.rates.count() == 1 and copy.swap_deltas.count() == 1 and copy.roof_addons.count() == 1
        taken = client.post(URL, {"name": "copy"}, format="json")
        assert taken.status_code == 409 and taken.json()["code"] == "market_rate_set_name_taken"
        renamed = client.patch(detail(copy), {"name": "Q4", "expected_version": 1}, format="json")
        assert renamed.status_code == 200 and renamed.json()["name"] == "Q4"
        assert client.patch(detail(copy), {"name": " "}, format="json").status_code == 400

    def test_swap_deltas_and_roof_addons(self, client):
        rate_set = MarketRateSetFactory()
        a, b = ComponentFactory(), ComponentFactory()
        payload = {"swap_deltas": [{"system_type": "ONGRID", "tier": "VALUE", "slot": "panel", "from_component_uid": str(a.uid), "to_component_uid": str(b.uid), "delta_incl_gst": "-1808"}]}
        response = client.put(detail(rate_set, "swap-deltas/"), payload, format="json")
        assert response.status_code == 200 and response.json()["outcome"]["created"] == 1
        rows = client.get(detail(rate_set, "swap-deltas/")).json()["results"]
        assert rows[0]["from_component"]["sku"] == a.sku and rows[0]["delta_incl_gst"] == "-1808.00"
        same = {"swap_deltas": [{**payload["swap_deltas"][0], "to_component_uid": str(a.uid)}]}
        assert client.put(detail(rate_set, "swap-deltas/"), same, format="json").status_code == 400
        addons = {"roof_addons": [{"structure_type": "SHEET_ROOF", "size_kw": "3", "addon_incl_gst": "14747"}]}
        assert client.put(detail(rate_set, "roof-addons/"), addons, format="json").json()["outcome"]["created"] == 1
        assert client.put(detail(rate_set, "roof-addons/"), addons, format="json").json()["outcome"]["unchanged"] == 1
        assert client.get(detail(rate_set, "roof-addons/")).json()["results"][0]["addon_incl_gst"] == "14747.00"

    def test_list_query_budget(self, client, django_assert_max_num_queries):
        for _ in range(12):
            rate_set = MarketRateSetFactory()
            MarketRateFactory(set=rate_set)
        with django_assert_max_num_queries(10):
            body = client.get(URL, {"page_size": 50}).json()
        assert body["count"] == 12 and body["results"][0]["rate_count"] == 1

    def test_children_query_budget(self, client, django_assert_max_num_queries):
        rate_set = MarketRateSetFactory()
        for size in range(1, 13):
            MarketRateFactory(set=rate_set, size_key=str(size), size_kw=Decimal(size))
            SwapDeltaFactory(set=rate_set)
            RoofAddonFactory(set=rate_set, size_kw=Decimal(size))
        for suffix in ("rates/", "swap-deltas/", "roof-addons/"):
            with django_assert_max_num_queries(10):
                assert client.get(detail(rate_set, suffix), {"page_size": 50}).json()["count"] == 12

    def test_put_on_the_set_itself_is_not_allowed(self, client, viewer):
        rate_set = MarketRateSetFactory()
        assert client.put(detail(rate_set), {"name": "x"}, format="json").status_code == 405
        assert viewer.put(detail(rate_set), {"name": "x"}, format="json").status_code == 403
        assert client.patch(detail(rate_set), {"note": "x", "expected_version": 1}, format="json").status_code == 200


def test_rate_keys_follow_flarize():
    assert rate_key(MarketRateFactory.build(system_type="HYBRID", tier="BASE", battery_config="1", future_size_key="10")) == "hybrid_base_1_up10"
    assert rate_key(MarketRateFactory.build(system_type="UPGRADE", tier="", from_size_key="5", size_key="8")) == "upgrade_5_8"
