"""Service-level behaviour: registrations (catalog usage, EMI provider, dashboard), outbox handlers, legacy import."""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from django.test import override_settings

from catalog.services import usage
from core.errors import Conflict
from core.models import LegacyMap
from packs.events import component_deleted, component_status_changed, mark_drafts_stale
from packs.models import ConfigPack, ConfigPin, ConfigStatus, ConfigVersion, PackRelease
from packs.services import legacy_import, maintenance, registrations, releases, versions
from packs.tests import factories

pytestmark = pytest.mark.django_db
price_sources = importlib.import_module("emi.services.price_sources")  # packs may not import website content (import-linter)


class FakeEvent:
    def __init__(self, payload):
        self.payload = payload


def _store(cfg=None, *, draft_status="DRAFT"):
    cfg = cfg or factories.config()
    draft_cfg = factories.config()
    draft_cfg["marketRates"] = {"ongrid_value": {"3": 239000}}
    return {
        "schema": "flarize.pack-config/1",
        "approved": {
            "version": 3,
            "approvedBy": "admin-001",
            "approvedAt": "2026-09-13T15:43:04.268Z",
            "note": "direct approve by Admin",
            "submittedBy": "admin-001",
            "submittedAt": "2026-09-13T15:43:04.268Z",
            "config": cfg,
        },
        "draft": {
            "version": 4,
            "status": draft_status,
            "basedOn": 3,
            "config": draft_cfg,
            "changeLog": [{"at": "2026-09-14T10:00:00Z", "by": "admin-001", "section": "marketRates", "note": None, "changed": True}],
        },
        "history": [
            {"version": 1, "approvedBy": "SEED_FROM_CATALOG", "approvedAt": "2026-09-08T07:50:49.474Z", "note": "Seeded from catalog."},
            {"version": 2, "approvedBy": "admin-001", "approvedAt": "2026-09-08T10:16:37.000Z", "note": "v2", "changes": 1},
            {"version": 3, "approvedBy": "admin-001", "approvedAt": "2026-09-13T15:43:04.268Z", "note": "direct approve by Admin", "changes": 3},
        ],
    }


REGISTRY = {
    "packages": [
        {
            "packageId": "P1",
            "systemType": "ongrid",
            "size": "5",
            "tier": "value",
            "phase": "1P",
            "components": [
                {"role": "panel", "componentId": "pnl2", "derivedBy": "explicit", "approvedAlternates": ["pnl1"]},
                {"role": "inverter", "componentId": "ghost"},
                {"role": "fixed", "componentId": "x"},
            ],
        }
    ]
}


class TestLegacyImport:
    def test_import_is_idempotent_and_traceable(self):
        factories.catalog()
        first = legacy_import.import_flarize_pack_config(_store(), REGISTRY)
        assert first["counts"]["pack-config.json:versions"]["created"] == 4
        statuses = dict(ConfigVersion.objects.values_list("number", "status"))
        assert statuses == {1: "SUPERSEDED", 2: "SUPERSEDED", 3: "APPROVED", 4: "DRAFT"}
        assert ConfigVersion.objects.get(number=1).config is None and ConfigVersion.objects.get(number=2).legacy_actor == "admin-001"
        draft = ConfigVersion.objects.get(number=4)
        assert draft.based_on.number == 3 and draft.change_log[0]["section"] == "marketRates"
        pin = ConfigPin.objects.get(pack__config_version__number=3, pack__key="ongrid-value-5")
        assert pin.component.sku == "pnl2" and pin.authoritative and pin.alternates == ["pnl1"]
        assert ConfigPack.objects.get(config_version__number=3, key="ongrid-value-5").panel.sku == "pnl2"
        assert ConfigPack.objects.get(config_version__number=3, key="ongrid-value-3-up5").panel.sku == "pnl1"  # registry keyed by system size 5, panel slot keyed by panel size 3: the pin wins
        assert any(v["code"] == "unknown_component" for v in first["violations"])
        assert LegacyMap.objects.filter(source_table="pack-config.json:versions").count() == 4
        again = legacy_import.import_flarize_pack_config(_store(), REGISTRY)
        assert again["counts"]["pack-config.json:versions"]["created"] == 0 and ConfigVersion.objects.count() == 4
        assert ConfigPin.objects.filter(pack__config_version__number=3).count() == 2  # ongrid-value-5 and the 3 → 5 future-ready pack

    def test_dry_run_writes_nothing(self):
        factories.catalog()
        result = legacy_import.import_flarize_pack_config(_store(), None, dry_run=True)
        assert result["counts"]["pack-config.json:versions"]["created"] == 4 and ConfigVersion.objects.count() == 0

    def test_violations(self):
        factories.catalog()
        ConfigVersion.objects.create(number=2, status=ConfigStatus.SUPERSEDED)
        store = _store()
        store["draft"]["config"] = {"marketRate": {}}
        store["history"].append({"version": "x"})
        result = legacy_import.import_flarize_pack_config(store, None)
        codes = {v["code"] for v in result["violations"]}
        assert {"number_taken", "invalid_config", "invalid_value"} <= codes
        assert not ConfigVersion.objects.filter(number=4).exists()

    def test_platform_moved_on_is_kept(self):
        factories.catalog()
        legacy_import.import_flarize_pack_config(_store(), None)
        draft = ConfigVersion.objects.get(number=4)
        versions.submit(draft, user=None)
        versions.approve(draft, user=None)
        result = legacy_import.import_flarize_pack_config(_store(), None)
        assert any(v["code"] == "target_moved_on" for v in result["violations"])
        assert ConfigVersion.objects.get(number=4).status == ConfigStatus.APPROVED
        assert ConfigVersion.objects.get(number=3).status == ConfigStatus.SUPERSEDED

    def test_publish_initial_releases(self):
        from pricing.tests.factories import gst_config

        factories.catalog()
        gst_config()
        with pytest.raises(Conflict):
            legacy_import.publish_initial_releases()
        legacy_import.import_flarize_pack_config(_store(), None)
        result = legacy_import.publish_initial_releases()
        assert result["price_release"] == 1 and result["pack_release"] == 1 and result["published"] is True
        assert result["market_rate_set"]["outcome"] == "activated"
        assert result["report"]["summary"]["packs_ready"] == 1
        again = legacy_import.publish_initial_releases()
        assert again["published"] is False and again["pack_release"] == 1 and again["market_rate_set"]["outcome"] == "unchanged"
        assert PackRelease.objects.count() == 1


class TestRegistrations:
    def test_usage_blocks_deletion(self, world):
        sections = {section.name: section for section in usage.usage_of(world["pnl1"])}
        assert sections["packs.config_versions"].count == 1
        assert sections["packs.current_release"].count == 0
        releases.publish(user=None)
        sections = {section.name: section for section in usage.usage_of(world["pnl1"])}
        assert sections["packs.current_release"].count == 1
        with pytest.raises(Conflict):
            usage.ensure_not_in_use(world["pnl1"])

    @override_settings(EMI_PRICE_SOURCE="PACK_RELEASE")
    def test_emi_provider(self, world):
        registrations.register()
        assert price_sources.active_sizes() == []
        releases.publish(user=None)
        (option,) = price_sources.active_sizes()
        assert option.uid == "ongrid-value-3" and option.system_cost == Decimal("229000.00") and option.capacity_kw == Decimal("3.00")
        assert option.price_per_kw == Decimal("76333.33") and price_sources.cache_namespaces() == ("packs",)

    def test_dashboard_counts(self, world, admin_user):
        assert registrations.pack_counts(admin_user) == {"current_release": 0, "open_drafts": 0, "submitted": 0}


class TestEvents:
    def test_price_release_marks_the_draft_stale_once(self, world):
        assert maintenance.price_release_published(number=2) is None  # no open draft
        draft = versions.create_draft(user=None, data={})
        mark_drafts_stale(FakeEvent({"number": 2}))
        mark_drafts_stale(FakeEvent({"number": 2}))
        draft.refresh_from_db()
        assert [entry.get("note") for entry in draft.change_log if entry.get("stale")] == ["PriceRelease #2 published"]

    def test_component_changes_refresh_the_mirror(self, world):
        assert maintenance.refresh_open_draft(reason="x") is None
        draft = versions.create_draft(user=None, data={})
        world["pnl1"].status = "RETIRED"
        world["pnl1"].save()
        component_status_changed(FakeEvent({"sku": "pnl1", "to": "RETIRED"}))
        assert ConfigPack.objects.get(config_version=draft, key="ongrid-value-3").panel.sku == "pnl2"
        component_deleted(FakeEvent({"sku": "pnl1"}))
