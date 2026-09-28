"""catalog.services.legacy_import — idempotent imports of the main backend and Flarize catalogs (PLAN §7.3/§7.4)."""

from decimal import Decimal

import pytest

from audit.models import AuditLog
from catalog.models import BatterySpec, Brand, Category, Component, ComponentChange, ComponentPublicProfile, ComponentStatus, ComponentTier, InverterSpec, PanelSpec
from catalog.services import legacy_import
from catalog.services.legacy_support import model_key
from catalog.tests.factories import BrandFactory, inverter, panel
from catalog.tests.legacy_fixtures import flarize_battery_master, flarize_catalog, goldenapp, import_all
from core.models import LegacyMap

pytestmark = pytest.mark.django_db
COUNT_KEYS = ("created", "updated", "unchanged", "skipped")


def _state() -> dict:
    """A comparable snapshot of every component (order-independent)."""
    state = {}
    for component in Component.objects.select_related("category", "brand", "panel_spec", "inverter_spec", "battery_spec"):
        spec = next((getattr(component, name) for name in ("panel_spec", "inverter_spec", "battery_spec") if hasattr(component, name)), None)
        state[component.sku.lower() if component.sku[0].islower() else component.name] = (
            component.category.slug,
            component.brand.name.casefold() if component.brand else None,
            component.brand_label,
            component.name,
            component.status,
            tuple(sorted(component.tiers.values_list("tier", flat=True))),
            str(getattr(spec, "wattage_w", None) or getattr(spec, "kw", None) or getattr(spec, "capacity_kwh", None)),
            component.is_public,
        )
    return state


class TestFullImport:
    def test_counts_mapping_and_idempotency(self):
        results = import_all(order=("flarize", "bom", "website"))
        flarize, bom, website = results["flarize"], results["bom"], results["website"]
        assert flarize["counts"]["catalog.json:categories"]["created"] == 31 and flarize["counts"]["catalog.json:items"]["created"] == 258
        assert bom["counts"]["bom_catalogitem"] == {"created": 2, "updated": 0, "unchanged": 255, "skipped": 0}
        assert website["counts"] == {
            "solar_panels": {"created": 11, "updated": 0, "unchanged": 0, "skipped": 0},
            "solar_inverters": {"created": 7, "updated": 0, "unchanged": 0, "skipped": 0},
            "batteries": {"created": 4, "updated": 0, "unchanged": 0, "skipped": 0},
        }
        assert Component.objects.count() == 258 + 2 + 22 and Category.objects.count() == 31
        assert LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_catalogitem").count() == 257
        assert LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_itemtier").count() > 0
        assert LegacyMap.objects.filter(source_system="FLARIZE", source_table="battery-master.json:batteries").count() == 0  # the overlay lands on the item's component
        assert AuditLog.objects.filter(action="catalog.legacy_import").count() == 3
        before = _state()
        changes = ComponentChange.objects.count()
        again = import_all(order=("flarize", "bom", "website"))
        for name in ("flarize", "bom", "website"):
            assert again[name]["created"] == 0 and again[name]["updated"] == 0, name
        assert _state() == before and ComponentChange.objects.count() == changes
        assert Component.objects.count() == 282 and Brand.objects.filter(name__iexact="waaree").count() == 1

    def test_import_order_does_not_change_the_result(self):
        import_all(order=("flarize", "bom", "website"))
        first = _state()
        Component.all_objects.all().delete()
        from catalog.models import Brand as BrandModel

        ComponentTier.all_objects.all().delete()
        BrandModel.all_objects.all().delete()
        Category.all_objects.all().delete()
        LegacyMap.objects.all().delete()
        import_all(order=("bom", "website", "flarize"))
        assert _state() == first

    def test_website_products_become_public_published_components(self):
        import_all(order=("website",))
        profile = ComponentPublicProfile.objects.select_related("component__panel_spec").get(headline="Ahnay Bi-55-550")
        component = profile.component
        assert profile.status == "PUBLISHED" and component.is_public and component.status == ComponentStatus.ACTIVE
        assert component.name == "Waaree Ahnay Bi-55-550 - 550Wp" and component.model == "Ahnay Bi-55-550" and component.brand_label == "Waaree"
        assert component.panel_spec.panel_type == "BIFACIAL" and component.panel_spec.technology == "P_TYPE_PERC" and component.panel_spec.efficiency_pct == Decimal("21.36")
        assert profile.overall_rating == "EXCELLENT" and profile.ratings == {"efficiency": 93, "heat_performance": 86, "warranty": 96, "kerala_climate": 96}
        assert profile.published_at == profile.created_at and profile.slug == "waaree-ahnay-bi-55-550"
        enphase = InverterSpec.objects.get(component__model="IQ8P Microinverter")
        assert enphase.kw == Decimal("0.475") and enphase.topology == "MICRO" and enphase.max_dc_input_kw == Decimal("0.670")
        battery = BatterySpec.objects.get(capacity_kwh=Decimal("4.61"))
        assert battery.backup_hours == Decimal("4.30") and battery.component.brand is None and battery.component.name == "Battery 4.61kWh - 4.30h backup"

    def test_prices_are_returned_not_written(self):
        results = import_all(order=("flarize", "bom", "website"))
        flarize_p1 = next(price for price in results["flarize"]["prices"] if price["sku"] == "p1")
        assert flarize_p1 == {"sku": "p1", "kind": "LIST", "amount": "13122.00", "per_watt": "24.3000", "source_system": "FLARIZE", "source_table": "catalog.json:items", "source_id": "p1"}
        assert {price["amount"] for price in results["website"]["prices"]} == {"140300.00", "145000.00", "151400.00"}
        assert len(results["bom"]["prices"]) == 257
        prices, differences = legacy_import.merge_prices(results["flarize"]["prices"], results["bom"]["prices"])
        by_sku = {price["sku"]: price for price in prices}
        assert by_sku["p2"]["source_system"] == "FLARIZE" and by_sku["ac_a0473dd3b8ec"]["source_system"] == "BACKEND"
        assert {"sku": "p2", "kind": "LIST", "bom": {"amount": "14300.00", "per_watt": "26.0000"}, "flarize": {"amount": "13585.00", "per_watt": "24.7000"}} in differences


class TestD2FlarizeWins:
    @pytest.mark.parametrize("order", [("flarize", "bom"), ("bom", "flarize")])
    def test_flarize_values_win_and_differences_are_reported(self, order):
        results = import_all(order=order)
        cb4 = Component.objects.get(sku="cb4")
        assert cb4.name == "CB Rod 14mm 250u 1.2m" and sorted(cb4.tiers.values_list("tier", flat=True)) == ["VALUE"]
        ec1 = Component.objects.select_related("brand").get(sku="ec1")
        assert ec1.brand.name == "FLEXGUARD" and ec1.brand_label == "FLEXGUARD"
        reporter = results[order[1]]
        conflicts = {v["sku"]: v for v in reporter["violations"] if v["code"] == "d2_flarize_wins"}
        assert set(conflicts) == {"p1", "pa_9e35d4adf2a3", "m2", "dc3", "is1", "cb4", "cb5", "cb6", "ec1", "la1"}
        fields = {d["field"] for d in conflicts["ec1"]["differences"]}
        assert {"name", "brand_label", "brand"} <= fields and all(v["severity"] == "warning" for v in conflicts.values())
        difference = next(d for d in conflicts["cb4"]["differences"] if d["field"] == "name")
        assert difference["flarize"] == "CB Rod 14mm 250u 1.2m" and difference["bom"] == "CB Rod 14mm 250u 1.0m"


class TestFlarizeMapping:
    def test_specs_attributes_status_and_battery_master(self):
        result = legacy_import.import_flarize_catalog(flarize_catalog(), flarize_battery_master())
        p1 = PanelSpec.objects.get(component__sku="p1")
        assert (p1.wattage_w, p1.is_dcr, p1.voc_v, p1.temperature_coefficient, p1.cell_count, p1.max_system_voltage_v) == (540, True, Decimal("49.60"), Decimal("-0.350"), 144, 1500)
        i35 = InverterSpec.objects.get(component__sku="i35")
        assert i35.topology == "OPTIMIZED_STRING" and i35.inverter_type == "ONGRID" and i35.max_isc_a == Decimal("12.00")
        assert Component.objects.get(sku="hm1").attributes == {"modules": 4, "device_type": "microinverter", "panels_per_device": 4, "micro_accessory_role": "microinverter"}
        hoymiles = Category.objects.get(slug="hoymiles")
        assert hoymiles.attribute_schema["properties"]["panels_per_device"]["type"] == ["integer", "null"] or hoymiles.attribute_schema["properties"]["panels_per_device"]["type"] == "integer"
        placeholder = Component.objects.get(sku="is_9b53be37fab1")
        assert placeholder.status == ComponentStatus.RETIRED and placeholder.engineering_status == "TEST_PLACEHOLDER_DO_NOT_USE" and placeholder.notes.startswith("Placeholder")
        bt1 = BatterySpec.objects.get(component__sku="bt1")
        assert bt1.engineering_status == "PENDING_ENGINEERING_APPROVAL" and bt1.external_protection_required is True and bt1.compatible_inverters is None
        assert bt1.compatible_system_types == ["hybrid"] and "exactModel" in bt1.open_items and bt1.status_history[1]["status"] == "PENDING_ENGINEERING_APPROVAL"
        assert Component.objects.get(sku="bt1").status == ComponentStatus.ACTIVE
        rejected = Component.objects.get(sku="ba_3b51aef23f0b")
        assert rejected.status == ComponentStatus.RETIRED and "REJECTED" in rejected.retired_reason
        assert BatterySpec.objects.get(component__sku="bt2").architecture == "ENPHASE"
        e2e = Component.objects.get(sku="pa_6a2a92fe02b0")
        assert e2e.warranty_text == "25 Years" and e2e.created_at.isoformat().startswith("2026-09-09")
        assert ComponentChange.objects.filter(component=e2e, field="source.created").count() == 1
        # ug_cable is measured in metres already; the battery cable category counts pieces, its item is sold per metre.
        assert Component.objects.get(sku="ug_2core").unit_override == "" and Component.objects.get(sku="bc_25").unit_override == "M"
        assert result["violations"] == []

    def test_battery_master_for_an_unknown_item_is_a_violation(self):
        master = {"batteries": {"bt99": {"componentId": "bt99", "engineeringStatus": "APPROVED"}}}
        result = legacy_import.import_flarize_catalog({"categories": {}}, master)
        assert result["violations"][0]["code"] == "unknown_component" and result["skipped"] == 1

    def test_invalid_values_reject_only_their_row(self):
        catalog = {
            "categories": {
                "panel": {
                    "label": "Solar Panel",
                    "gstDefault": 5,
                    "items": [
                        {"id": "p1", "name": "Good 540W", "brand": "Waaree", "tiers": ["base"], "watt": 540, "price": 1, "approvalStatus": "APPROVED"},
                        {"id": "p2", "name": "Bad", "brand": "Waaree", "tiers": ["gold"], "watt": 540, "price": 1},
                        {"id": "p3", "name": "Bad voc", "brand": "Waaree", "tiers": [], "watt": 540, "voc": 49.123, "price": 1},
                        {"id": "p4", "name": "No watt", "brand": "Waaree", "tiers": [], "price": 1},
                    ],
                },
                "weird": {"label": "Weird", "gstDefault": "abc", "items": [{"id": "w1", "name": "W"}]},
            }
        }
        result = legacy_import.import_flarize_catalog(catalog, None)
        codes = {(v["source_id"], v["code"]) for v in result["violations"]}
        assert ("p2", "invalid_value") in codes and ("p3", "invalid_value") in codes and ("p4", "spec_missing") in codes
        assert ("weird", "invalid_value") in codes and ("w1", "category_not_imported") in codes
        assert dict(Component.objects.values_list("sku", "status")) == {"p1": "ACTIVE", "p4": "DRAFT"}

    def test_status_mapping(self):
        catalog = {
            "categories": {
                "dcdb": {
                    "label": "DCDB",
                    "gstDefault": 18,
                    "items": [
                        {"id": "d1", "name": "A", "tiers": [], "price": 1, "approvalStatus": "PENDING"},
                        {"id": "d2", "name": "B", "tiers": [], "price": 1, "approvalStatus": "REJECTED"},
                        {"id": "d3", "name": "C", "tiers": [], "price": 1, "status": "INACTIVE"},
                        {"id": "d4", "name": "D", "tiers": [], "price": 1, "unit": "kg", "gstOverride": 12},
                    ],
                },
                "mystery": {"label": "Mystery", "gstDefault": 18, "items": []},
            }
        }
        result = legacy_import.import_flarize_catalog(catalog, {})
        statuses = dict(Component.objects.values_list("sku", "status"))
        assert statuses == {"d1": "DRAFT", "d2": "RETIRED", "d3": "RETIRED", "d4": "ACTIVE"}
        d4 = Component.objects.get(sku="d4")
        assert d4.unit_override == "KG" and d4.gst_rate_override == Decimal("0.1200") and d4.brand is None
        assert Category.objects.get(slug="mystery").bom_role == "MISC" and any(v["code"] == "unknown_category_role" for v in result["violations"])

    def test_change_log_import_is_not_duplicated_and_status_follows_source(self):
        catalog = {"categories": {"panel": flarize_catalog()["categories"]["panel"]}}
        legacy_import.import_flarize_catalog(catalog, None)
        p1 = Component.objects.get(sku="p1")
        catalog["categories"]["panel"]["items"][0]["status"] = "INACTIVE"
        second = legacy_import.import_flarize_catalog(catalog, None)
        p1.refresh_from_db()
        assert p1.status == ComponentStatus.RETIRED and second["counts"]["catalog.json:items"]["updated"] == 1
        assert ComponentChange.objects.filter(component__sku="pa_6a2a92fe02b0", field="source.created").count() == 1


class TestBomMapping:
    def test_bom_only_rows_tiers_and_timestamps(self):
        result = legacy_import.import_bom_catalog(goldenapp("bom_category"), goldenapp("bom_catalogitem"), goldenapp("bom_itemtier"))
        assert result["violations"] == [] and result["created"] == 31 + 257
        p2 = Component.objects.get(sku="p2")
        assert p2.panel_spec.wattage_w == 550 and p2.panel_spec.is_dcr is True and sorted(p2.tiers.values_list("tier", flat=True)) == ["VALUE"]
        i1 = InverterSpec.objects.get(component__sku="i1")
        assert (i1.kw, i1.phase, i1.inverter_type) == (Decimal("3.000"), "1P", "ONGRID") and i1.component.model == "MIC 3000TL-X"
        hybrid_acdbs = Component.objects.filter(category__slug="acdb", attributes__type="hybrid")
        assert hybrid_acdbs.count() == 2 and all(acdb.attributes["phase"].endswith("HYB") for acdb in hybrid_acdbs)
        assert Category.objects.get(slug="dc_cable").unit == "M" and Category.objects.get(slug="panel").gst_rate == Decimal("0.0500")
        tier_map = LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_itemtier")
        assert tier_map.count() == len(goldenapp("bom_itemtier"))

    def test_unknown_references_are_violations(self):
        result = legacy_import.import_bom_catalog(
            [{"id": 1, "slug": "panel", "label": "Solar Panel", "gst_default": 5}],
            [
                {"id": 10, "item_id": "p1", "category_id": 99, "name": "x", "brand": "y", "price": "1.00"},
                {"id": 11, "item_id": "p2", "category_id": 1, "name": "x", "brand": "y", "price": "1.00", "kw": "3.00"},
            ],
            [{"id": 5, "item_id": 404, "tier": "base"}],
        )
        codes = sorted(v["code"] for v in result["violations"])
        assert codes == ["invalid_value", "unknown_category", "unknown_item"]

    def test_deleted_targets_are_not_recreated(self):
        items = goldenapp("bom_catalogitem")[:5]
        tiers = [row for row in goldenapp("bom_itemtier") if row["item_id"] in {item["id"] for item in items}]
        legacy_import.import_bom_catalog(goldenapp("bom_category"), items, tiers)
        Component.objects.get(sku="p1").soft_delete()
        result = legacy_import.import_bom_catalog(goldenapp("bom_category"), items, tiers)
        assert any(v["code"] == "target_deleted" and v["severity"] == "warning" for v in result["violations"])
        assert Component.all_objects.filter(sku__iexact="p1").count() == 1


class TestWebsiteMatching:
    def _panel_row(self, **overrides):
        row = dict(goldenapp("solar_panels")[0])
        row.update(overrides)
        return row

    def test_exact_match_merges_and_reports_conflicts(self):
        brand = BrandFactory(name="Waaree")
        existing = panel(sku="p77", brand=brand, brand_label="WAAREE", model="AHNAY bi 55/550", spec={"wattage_w": 550, "efficiency_pct": Decimal("21.00")})
        LegacyMap.objects.create(source_system="FLARIZE", source_table="catalog.json:items", source_id="p77", target_table="catalog_component", target_id=existing.pk)
        result = legacy_import.import_goldenray_products([self._panel_row()], [], [])
        assert Component.objects.count() == 1 and result["counts"]["solar_panels"]["updated"] == 1
        existing.refresh_from_db()
        spec = PanelSpec.objects.get(component=existing)
        assert existing.is_public and spec.efficiency_pct == Decimal("21.00") and spec.panel_type == "BIFACIAL"
        codes = {v["code"] for v in result["violations"]}
        assert {"value_conflict", "brand_label_differs"} <= codes
        assert existing.public_profile.headline == "Ahnay Bi-55-550" and existing.public_profile.status == "PUBLISHED"

    def test_ambiguous_match_is_not_imported(self):
        brand = BrandFactory(name="Waaree")
        panel(sku="p1", brand=brand, model="Ahnay Bi-55-550", spec={"wattage_w": 550})
        panel(sku="p2", brand=brand, model="AHNAY-BI-55-550", spec={"wattage_w": 550})
        result = legacy_import.import_goldenray_products([self._panel_row()], [], [])
        assert result["violations"][0]["code"] == "ambiguous_match" and result["violations"][0]["candidates"] == ["p1", "p2"]
        assert result["skipped"] == 1 and not ComponentPublicProfile.objects.exists()

    def test_same_table_duplicates_never_merge(self):
        rows = [self._panel_row(id=1), self._panel_row(id=2)]
        legacy_import.import_goldenray_products(rows, [], [])
        assert Component.objects.filter(category__slug="panel").count() == 2
        assert ComponentPublicProfile.objects.filter(slug__startswith="waaree-ahnay").count() == 2

    def test_inverter_match_on_brand_model_and_kw(self):
        brand = BrandFactory(name="Sungrow")
        existing = inverter(sku="i77", brand=brand, model="SG5.0RS", spec={"kw": Decimal("5.000")})
        row = next(row for row in goldenapp("solar_inverters") if row["name"] == "SG5.0RS")  # Sungrow, 5000 W
        assert row["name"] == "SG5.0RS" and model_key(row["name"]) == "sg50rs"
        legacy_import.import_goldenray_products([], [row], [])
        assert Component.objects.count() == 1 and InverterSpec.objects.get(component=existing).brand_trust == "Excellent"

    def test_bad_enum_value_is_rejected(self):
        result = legacy_import.import_goldenray_products([self._panel_row(technology="quantum")], [], [])
        assert result["violations"][0]["code"] == "invalid_value" and result["violations"][0]["column"] == "technology"

    def test_dry_run_writes_nothing(self):
        result = legacy_import.import_goldenray_products(goldenapp("solar_panels"), goldenapp("solar_inverters"), goldenapp("batteries"), dry_run=True)
        assert result["created"] == 22 and not Component.objects.exists() and not LegacyMap.objects.exists()
        assert not AuditLog.objects.filter(action="catalog.legacy_import").exists()

    def test_retired_match_keeps_profile_unpublished(self):
        brand = BrandFactory(name="Waaree")
        existing = panel(sku="p77", brand=brand, model="Ahnay Bi-55-550", status=ComponentStatus.RETIRED, spec={"wattage_w": 550})
        LegacyMap.objects.create(source_system="FLARIZE", source_table="catalog.json:items", source_id="p77", target_table="catalog_component", target_id=existing.pk)
        result = legacy_import.import_goldenray_products([self._panel_row()], [], [])
        assert any(v["code"] == "profile_not_published" for v in result["violations"])
        assert ComponentPublicProfile.objects.get(component=existing).status == "DRAFT"
