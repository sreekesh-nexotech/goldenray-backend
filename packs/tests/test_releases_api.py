"""``/api/v1/packs/releases/`` and ``/api/v1/packs/compare/``: preview (publish report), publish, reads, compare."""

from __future__ import annotations

from decimal import Decimal

import pytest

from catalog.models import ComponentStatus
from core.models import OutboxEvent
from engineering.models import Finding, Run
from engineering.services import runs
from packs.models import ConfigStatus, PackRelease, ReleasePack
from packs.services import releases
from pricing.models import MarketRate, PriceRelease

pytestmark = pytest.mark.django_db
BASE = "/api/v1/packs/releases/"


def _publish(client, **body):
    response = client.post(BASE, body, format="json")
    assert response.status_code == 201, response.json()
    return response.json()


class TestAccess:
    def test_anonymous_and_forbidden(self, api_client, outsider, viewer, world):
        assert api_client.get(BASE).status_code == 401
        assert outsider.get(BASE).status_code == 403
        assert outsider.post(f"{BASE}preview/", {}, format="json").status_code == 403
        assert viewer.post(BASE, {}, format="json").status_code == 403
        assert api_client.get("/api/v1/packs/compare/?a=1&b=1").status_code == 401
        assert outsider.get("/api/v1/packs/compare/?a=1&b=1").status_code == 403


class TestPreview:
    def test_report_and_matrix(self, viewer, world):
        body = viewer.post(f"{BASE}preview/", {}, format="json").json()
        assert body["can_publish"] is True and body["next_number"] == 1 and body["current_number"] is None
        matrix = {row["key"]: row for row in body["matrix"]}
        assert matrix["ongrid-value-3"]["status"] == "READY" and matrix["ongrid-value-3"]["price"] == 229000
        assert matrix["ongrid-value-5"]["reasons"] == ["MARKET_RATE_NOT_SET"]
        assert body["summary"]["packs_ready"] == 1 and body["summary"]["packs_total"] == 3
        assert PackRelease.objects.count() == 0

    def test_global_blockers(self, admin, world):
        world["version"].versioned_update(None, status=ConfigStatus.SUPERSEDED)
        body = admin.post(f"{BASE}preview/", {}, format="json").json()
        assert [item["code"] for item in body["items"] if item["severity"] == "BLOCK"] == ["NO_APPROVED_VERSION"]
        response = admin.post(BASE, {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "publish_blocked"

    def test_no_price_release_and_no_rule_set(self, admin, world):
        from engineering.models import RuleSet

        PriceRelease.objects.update(status="SUPERSEDED")
        RuleSet.objects.update(active=False)
        codes = {item["code"] for item in admin.post(f"{BASE}preview/", {}, format="json").json()["items"]}
        assert {"NO_PRICE_RELEASE", "NO_ACTIVE_RULE_SET"} <= codes

    def test_no_publishable_pack(self, admin, world):
        MarketRate.objects.update(customer_price_incl_gst=0)
        world["price_release"].payload["market_rates_by_key"] = {"ongrid_value": {"3": "0"}}
        world["price_release"].save()
        body = admin.post(f"{BASE}preview/", {}, format="json").json()
        assert "NO_PUBLISHABLE_PACKS" in [item["code"] for item in body["items"]] and body["can_publish"] is False

    def test_retired_component_excludes_the_pack(self, admin, world):
        world["price_release"].payload["market_rates_by_key"]["ongrid_value"]["5"] = "300000"
        world["price_release"].save()
        world["is1"].status = ComponentStatus.RETIRED
        world["is1"].save()
        body = admin.post(f"{BASE}preview/", {}, format="json").json()
        codes = {item["code"] for item in body["items"]}
        # the retired isolator is no longer eligible: the checker then misses the AC isolator
        assert "PACK_ENGINEERING_BLOCKED" in codes

    def test_pins_to_retired_components_are_reported(self, admin, world):
        from packs.models import ConfigPack, ConfigPin

        pack = ConfigPack.objects.get(config_version=world["version"], key="ongrid-value-3")
        ConfigPin.objects.create(pack=pack, slot_key="panel", component=world["pnl2"])
        world["pnl2"].status = ComponentStatus.RETIRED
        world["pnl2"].save()
        codes = {item["code"] for item in admin.post(f"{BASE}preview/", {}, format="json").json()["items"]}
        assert "PIN_COMPONENT_NOT_SELECTABLE" in codes

    def test_gst_and_market_rate_differences_are_reported(self, admin, world):
        world["price_release"].payload["gst"] = {"effective_rate_pct": "12"}
        world["price_release"].payload["market_rates_by_key"]["ongrid_value"]["3"] = "239000"
        world["price_release"].save()
        body = admin.post(f"{BASE}preview/", {}, format="json").json()
        codes = {item["code"] for item in body["items"]}
        assert {"CONFIG_GST_DIFFERS", "CONFIG_MARKET_RATES_DIFFER"} <= codes
        assert {row["key"]: row["price"] for row in body["matrix"]}["ongrid-value-3"] == 239000


class TestPublish:
    def test_publish_writes_the_release(self, admin, world, drain_outbox):
        body = _publish(admin, note="first")
        assert body["number"] == 1 and body["status"] == "PUBLISHED" and body["pack_count"] == 1
        assert body["config_version_number"] == 1 and body["price_release_number"] == 1
        pack = ReleasePack.objects.get()
        assert pack.customer_price_incl_gst == Decimal("229000.00") and pack.customer_price_excl_gst + pack.gst_amount == pack.customer_price_incl_gst
        assert [line["sku"] for line in pack.bom if line["source"] == "SLOT"] == ["pnl1", "inv1", "is1"]
        world["version"].refresh_from_db()
        assert world["version"].status == ConfigStatus.PUBLISHED
        assert Run.objects.filter(subject_uid=world["version"].uid).count() == 1
        event = OutboxEvent.objects.get(event_type="packs.release_published")
        assert event.payload["number"] == 1 and event.payload["packs"] == 1

    def test_unchanged_release_is_blocked_and_stale_number(self, admin, world):
        _publish(admin)
        again = admin.post(BASE, {}, format="json")
        assert again.status_code == 409 and "RELEASE_UNCHANGED" in again.json()["errors"]["report"][0]
        stale = admin.post(BASE, {"expected_current_number": 0}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    def test_second_release_supersedes_and_compare(self, admin, viewer, world):
        _publish(admin)
        world["price_release"].payload["market_rates_by_key"]["ongrid_value"]["3"] = "239000"
        world["price_release"].save()
        second = _publish(admin, expected_current_number=1)
        assert second["number"] == 2
        assert PackRelease.objects.get(number=1).status == "SUPERSEDED"
        compare = viewer.get("/api/v1/packs/compare/", {"a": 1, "b": 2}).json()
        assert compare["changed"][0]["key"] == "ongrid-value-3" and compare["changed"][0]["price_after"] == "239000.00"
        assert viewer.get("/api/v1/packs/compare/", {"a": "x", "b": 2}).status_code == 400
        assert viewer.get("/api/v1/packs/compare/", {"a": 1, "b": 9}).status_code == 404

    def test_waived_block_findings_let_the_pack_through(self, admin, world):
        from engineering.models import SubjectType

        world["is1"].status = ComponentStatus.RETIRED
        world["is1"].save()
        run = admin.post(f"/api/v1/packs/config-versions/{world['version'].uid}/run-checker/", {}, format="json").json()
        blockers = Finding.objects.filter(run__uid=run["uid"], severity="BLOCK", context__pack="ongrid-value-3")
        assert blockers.exists()
        for finding in blockers:
            runs.acknowledge(finding, user=None, reason="accepted by engineering")
        assert runs.acknowledged_identities(SubjectType.PACK_CONFIG_VERSION, world["version"].uid)
        body = admin.post(f"{BASE}preview/", {}, format="json").json()
        assert "PACK_ENGINEERING_WAIVED" in {item["code"] for item in body["items"]}
        assert {row["key"]: row["status"] for row in body["matrix"]}["ongrid-value-3"] == "READY"


class TestRead:
    def test_list_detail_current_packs(self, admin, viewer, world, django_assert_max_num_queries):
        assert viewer.get(f"{BASE}current/").json()["code"] == "no_current_release"
        _publish(admin)
        with django_assert_max_num_queries(10):
            assert viewer.get(BASE).json()["count"] == 1
        assert viewer.get(f"{BASE}1/").json()["publish_report"]["can_publish"] is True
        assert viewer.get(f"{BASE}current/").json()["number"] == 1
        assert viewer.get(f"{BASE}7/").status_code == 404
        with django_assert_max_num_queries(10):
            packs = viewer.get(f"{BASE}1/packs/").json()["results"]
        assert packs[0]["key"] == "ongrid-value-3" and packs[0]["landed_cost_total"] is None and "internal" not in packs[0]["pricing"]
        internal = admin.get(f"{BASE}1/packs/", {"system_type": "ONGRID"}).json()["results"][0]
        assert internal["landed_cost_total"] is not None and "internal" in internal["pricing"]
        assert viewer.get(f"{BASE}1/packs/", {"tier": "BASE"}).json()["count"] == 0

    def test_release_payload_hash_ignores_number_and_time(self, admin, world):
        built = releases.build()
        assert "number" not in built.payload and built.sha256 == releases.build().sha256
