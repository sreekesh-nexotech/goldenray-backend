"""pricing/releases/ — preview (publish report), publish, current, detail (internal redaction), diff, website prices."""

from decimal import Decimal

import pytest
from django.db import transaction

from audit.models import AuditLog
from catalog.models import ComponentStatus
from catalog.tests.factories import CategoryFactory, ComponentFactory, published
from core.models import OutboxEvent
from flarize.cache_utils import get_versions
from pricing.models import PriceKind, PriceRelease, PriceReleaseLine, PriceSource, ReleaseStatus
from pricing.services import cost_config, releases
from pricing.services.prices import write_price
from pricing.tests.factories import CostConfigFactory, MarketRateFactory, MarketRateSetFactory, PriceFactory, SwapDeltaFactory, gst_config

pytestmark = pytest.mark.django_db
URL = "/api/v1/pricing/releases/"


def ready_data():
    """A publishable state: an ACTIVE set with one rate, GST config, a priced component."""
    rate_set = MarketRateSetFactory(status="ACTIVE")
    MarketRateFactory(set=rate_set)
    gst_config()
    category = CategoryFactory(gst_rate=Decimal("0.0500"))
    component = ComponentFactory(sku="p2", category=category)
    PriceFactory(component=component, kind=PriceKind.LIST, amount=Decimal("13585.00"))
    PriceFactory(component=component, kind=PriceKind.LANDED, amount=Decimal("9000.00"))
    return rate_set, component


def codes(report, severity=None):
    return {item["code"] for item in report["items"] if severity is None or item["severity"] == severity}


class TestPermissions:
    def test_anonymous_outsider_viewer(self, api_client, outsider, viewer):
        assert api_client.get(URL).status_code == 401
        assert api_client.post(f"{URL}preview/").status_code == 401
        assert outsider.get(URL).status_code == 403
        assert outsider.post(f"{URL}preview/").status_code == 403
        assert viewer.get(URL).status_code == 200
        assert viewer.post(f"{URL}preview/").status_code == 200
        assert viewer.post(URL, {}, format="json").status_code == 403


class TestPreviewAndPublish:
    def test_blockers_listed_and_publish_refused(self, client):
        report = client.post(f"{URL}preview/").json()
        assert report["can_publish"] is False
        assert {"MARKET_RATE_SET_MISSING", "GST_CONFIG_MISSING"} <= codes(report, "BLOCK")
        refused = client.post(URL, {}, format="json")
        assert refused.status_code == 409 and refused.json()["code"] == "publish_blocked"
        assert not PriceRelease.objects.exists()

    def test_warnings_do_not_block(self, client):
        ready_data()
        ComponentFactory(status=ComponentStatus.ACTIVE)  # no LIST, no LANDED
        zero = MarketRateFactory(set=MarketRateSetFactory(status="DRAFT"))
        zero.delete()
        report = client.post(f"{URL}preview/").json()
        assert report["can_publish"] is True and report["next_number"] == 1 and report["current_number"] is None
        assert {"LIST_PRICE_MISSING", "LANDED_COST_MISSING", "COST_CONFIG_MISSING", "INSTALLATION_MATRIX_EMPTY", "VALIDITY_POLICY_MISSING"} <= codes(report, "WARN")
        assert report["summary"]["components"] == 1 and report["changes"]["components"]["added"] == 1

    def test_publish_writes_release_lines_payload_and_event(self, client, pricing_user):
        rate_set, component = ready_data()
        before = get_versions(["pricing"])["pricing"]
        response = client.post(URL, {"note": "first", "expected_current_number": 0}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["number"] == 1 and body["status"] == "PUBLISHED" and body["market_rate_set"]["uid"] == str(rate_set.uid)
        assert body["payload"]["components"]["p2"]["list_price"] == "13585.00" and body["payload"]["components"]["p2"]["landed_cost"] == "9000.00"
        assert body["payload"]["gst"]["effective_rate_pct"] == "8.9"
        assert body["payload"]["market_rates_by_key"] == {"ongrid_value": {"3": "229000"}}
        release = PriceRelease.objects.get()
        assert releases.sha256_of(release.payload) == release.payload_sha256
        line = PriceReleaseLine.objects.get(release=release)
        assert (line.component_id, line.list_price, line.landed_cost, line.gst_rate) == (component.pk, Decimal("13585.00"), Decimal("9000.00"), Decimal("0.0500"))
        assert get_versions(["pricing"])["pricing"] > before
        event = OutboxEvent.objects.get(event_type="pricing.release_published")
        assert event.payload["number"] == 1 and event.payload["previous_number"] is None
        assert AuditLog.objects.get(action="pricing.release_published").actor == pricing_user

    def test_unchanged_release_is_blocked_and_next_supersedes(self, client):
        _, component = ready_data()
        assert client.post(URL, {}, format="json").status_code == 201
        unchanged = client.post(URL, {}, format="json")
        assert unchanged.status_code == 409 and "RELEASE_UNCHANGED" in unchanged.json()["errors"]["report"][0]
        stale = client.post(URL, {"expected_current_number": 0}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        with transaction.atomic():
            write_price(component, PriceKind.LIST, Decimal("13000.00"), user=None, source=PriceSource.MANUAL)
        response = client.post(URL, {"expected_current_number": 1}, format="json")
        assert response.status_code == 201 and response.json()["number"] == 2
        first = PriceRelease.objects.get(number=1)
        assert first.status == ReleaseStatus.SUPERSEDED and first.superseded_at is not None
        assert PriceRelease.objects.filter(status=ReleaseStatus.PUBLISHED).count() == 1
        diff = client.get(f"{URL}current/diff/").json()
        assert diff["current"] == 2 and diff["against"] == 1
        assert {"key": "p2", "field": "list_price", "old": "13585.00", "new": "13000.00"} in diff["sections"]["components"]["changed"]
        explicit = client.get(f"{URL}current/diff/", {"against": 2}).json()
        assert explicit["sections"]["components"]["changed"] == []
        assert client.get(f"{URL}current/diff/", {"against": "x"}).status_code == 400
        assert client.get(f"{URL}current/diff/", {"against": 9}).status_code == 404

    def test_detail_current_and_list(self, client, django_assert_max_num_queries):
        ready_data()
        client.post(URL, {}, format="json")
        assert client.get(f"{URL}1/").json()["number"] == 1
        assert client.get(f"{URL}current/").json()["number"] == 1
        assert client.get(f"{URL}9/").status_code == 404
        with django_assert_max_num_queries(10):
            rows = client.get(URL).json()["results"]
        assert rows[0]["summary"]["components"] == 1 and "payload" not in rows[0]

    def test_no_release_yet(self, client):
        assert client.get(f"{URL}current/").status_code == 404
        assert client.get(f"{URL}current/diff/").status_code == 404

    def test_landed_costs_and_margins_are_redacted(self, client, editor):
        ready_data()
        CostConfigFactory(key="target_gross_margin_by_tier", value={"BASE": {"target": 0.2}})
        client.post(URL, {}, format="json")
        hidden = editor.get(f"{URL}1/").json()["payload"]
        assert "landed_cost" not in hidden["components"]["p2"] and "target_gross_margin_by_tier" not in hidden["cost_config"]
        full = client.get(f"{URL}1/").json()["payload"]
        assert full["components"]["p2"]["landed_cost"] == "9000.00" and "target_gross_margin_by_tier" in full["cost_config"]

    def test_retired_components_and_swap_warnings(self, client):
        rate_set, _ = ready_data()
        retired = ComponentFactory(status=ComponentStatus.RETIRED)
        PriceFactory(component=retired)
        SwapDeltaFactory(set=rate_set, to_component=retired)
        report = client.post(f"{URL}preview/").json()
        assert "RETIRED_COMPONENTS_EXCLUDED" in codes(report, "INFO") and "SWAP_DELTA_RETIRED_COMPONENT" in codes(report, "WARN")

    def test_invalid_gst_split_blocks(self, client):
        ready_data()
        with transaction.atomic():
            cost_config.set_value("gst_goods_share", 0.6, user=None)
        report = client.post(f"{URL}preview/").json()
        assert "GST_CONFIG_INVALID" in codes(report, "BLOCK")


class TestWebsitePrices:
    def test_public_product_shows_the_release_price(self, client, api_client):
        _, component = ready_data()
        profile = published(component, price_range_label="₹12,000 - ₹15,000")
        url = f"/api/public/v1/products/{component.category.slug}/{profile.slug}/"
        before = api_client.get(url).json()
        assert before["price"] is None and before["price_range_label"] == "₹12,000 - ₹15,000"
        client.post(URL, {}, format="json")
        after = api_client.get(url).json()
        assert after["price"] == {"min": "14264.00", "max": "14264.00", "currency": "INR", "gst_inclusive": True, "release": 1}
        assert after["price_range_label"] == "₹14,264"
